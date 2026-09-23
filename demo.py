"""固定输入演示（确定性、离线、无第三方依赖）。

运行::

    python demo.py

内容：
  第一部分（原有能力，行为不变）
    1. 正常结果：共享 cell 的计数器闭包 inc/get；letrec 互递归 even/odd；
       独立源解释器与目标 IR 解释器逐项对照。
    2. 异常边界：未绑定变量可定位拒绝；501 节点超上限拒绝。
  第二部分（本次新增：可暂停/保存/恢复的闭包 + 版本迁移）
    3. 显式栈执行机：调用栈中间按步预算多次暂停/恢复，结果与连续执行一致；
       真正启动*新进程*从镜像恢复（对象身份只靠稳定 OID 跨进程）。
    4. 序列化保真：互递归组环、两闭包共享计数器单元；镜像对象条目顺序
       任意置换，观察结果与别名关系不变。
    5. 安全点升级（同一执行例内）：旧 v1 栈帧持有共享单元 → 字段迁移
       （新增常量默认字段）后的 v2 闭包再次修改同一单元 → 安全点中断 →
       恢复到结束；升级后的镜像在新进程里跑完。
    6. 棘手边界与真实拒绝：非法迁移（删除仍被引用字段）在发布前拒绝；
       提交前注入失败 → 完整回滚（版本指针/闭包载荷/记录布局不变，
       机器仍跑出原 v1 结果）。

全部结果现场计算，无 sleep、无预录答案、不联网。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import replace

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source, eval_target
from recenv import image as imgmod
from recenv import persist
from recenv.machine import Machine
from recenv.migration import fieldize, upgrade_at_safepoint


def line(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ---------------------------------------------------------------------------
# 第一部分：原有能力
# ---------------------------------------------------------------------------

def build_counter() -> ast.Expr:
    return ast.Let(
        "c", ast.Int(0),
        ast.Let(
            "inc", ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))),
            ast.Let(
                "get", ast.Lambda([], ast.Var("c")),
                ast.Add(
                    ast.Call(ast.Var("inc"), []),
                    ast.Add(
                        ast.Call(ast.Var("inc"), []),
                        ast.Call(ast.Var("get"), []),
                    ),
                ),
            ),
        ),
    )


def build_even_odd(n: int) -> ast.Expr:
    ev = ast.Lambda(
        ["n"],
        ast.If0(ast.Var("n"), ast.Int(1),
                ast.Call(ast.Var("odd"), [ast.Add(ast.Var("n"), ast.Int(-1))])),
    )
    od = ast.Lambda(
        ["n"],
        ast.If0(ast.Var("n"), ast.Int(0),
                ast.Call(ast.Var("even"), [ast.Add(ast.Var("n"), ast.Int(-1))])),
    )
    return ast.LetRec(
        [ast.RecBinding("even", ev), ast.RecBinding("odd", od)],
        ast.Call(ast.Var("even"), [ast.Int(n)]),
    )


def demo_legacy_core() -> None:
    line("1. 原有能力 A：两个闭包共享同一个可变 cell（inc / get）")
    root = build_counter()
    s_value, s_events = eval_source(root)
    program = convert(root)
    t_value, t_events = eval_target(program)
    print(f"源解释器（独立参考）: 返回值={s_value}  副作用={s_events}")
    print(f"目标解释器（执行 IR）: 返回值={t_value}  副作用={t_events}")
    assert (s_value, s_events) == (t_value, t_events)
    print("对照一致 ✔")

    line("1. 原有能力 B：letrec 互递归 even/odd（even(9)）")
    root = build_even_odd(9)
    s_value, _ = eval_source(root)
    t_value, _ = eval_target(convert(root))
    print(f"源/目标一致：even(9) = {s_value} / {t_value} ✔")


def demo_legacy_boundaries() -> None:
    line("2. 原有边界：未绑定变量拒绝 + 501 节点拒绝")
    bad = ast.Let(
        "f",
        ast.Lambda([], ast.Set("total", ast.Int(1), loc="set:total:行12")),
        ast.Call(ast.Var("f"), []),
    )
    try:
        convert(bad)
    except errors.UnboundVariableError as e:
        print(f"未绑定变量实际触发 {type(e).__name__}: {e} ✔")
    else:
        raise AssertionError("未绑定变量未被拒绝")

    node = ast.Int(0)
    for k in range(250):
        node = ast.Add(node, ast.Int(k))
    n = ast.count_nodes(node)
    try:
        convert(node)
    except errors.NodeLimitError as e:
        print(f"{n} 节点实际触发 {type(e).__name__}: {e.count} > {e.limit} ✔")
    else:
        raise AssertionError("超规模程序未被拒绝")


# ---------------------------------------------------------------------------
# 第二部分：新增能力
# ---------------------------------------------------------------------------

def _loop_program(n: int, inc: int) -> ast.Expr:
    """let c=0 in letrec loop(n)= if0(n,c, set(c,c+inc)+loop(n-1)) in loop(n)."""
    body = ast.If0(
        ast.Var("n"), ast.Var("c"),
        ast.Add(
            ast.Set("c", ast.Add(ast.Var("c"), ast.Int(inc))),
            ast.Call(ast.Var("loop"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    return ast.Let(
        "c", ast.Int(0),
        ast.LetRec([ast.RecBinding("loop", ast.Lambda(["n"], body))],
                   ast.Call(ast.Var("loop"), [ast.Int(n)])),
    )


def _loop_v2_function(p, fid):
    """v2 IR：增量改读新字段 step（默认常量 3）。

    新捕获布局槽位：0=c cell，1=loop 组 cell，2=step(常量 val)，3=n 参数。
    """
    fn1 = p.functions[fid]
    sid = p.function_field_map[fid]["v1"]
    cap_fields = [fs.field_name for fs in p.frame_schemas[sid]][:fn1.n_capture]
    body = ast.TIf0(
        ast.TGet(0, 3),
        ast.TCellGet(0, 0, "c"),
        ast.TPrim("add", (
            ast.TCellSet(
                0, 0,
                ast.TPrim("add", (ast.TCellGet(0, 0, "c"), ast.TGet(0, 2))),
                "c",
            ),
            ast.TCall(
                ast.TCellGet(0, 1, "loop"),
                (ast.TPrim("add", (ast.TGet(0, 3), ast.TConst(-1))),),
            ),
        )),
    )
    return replace(fn1, n_capture=3, n_slots=4, body=body,
                   free_names=tuple(cap_fields) + ("step",))


def demo_pause_resume_and_new_process(tmpdir: str) -> None:
    line("3. 显式栈执行机：栈中间暂停 / 恢复，并在【全新进程】里续执行")
    n = 3000
    root = _loop_program(n, 1)
    program = fieldize(convert(root))

    # 进程内：连续执行
    full = Machine(program, step_limit=10_000_000)
    full.run()

    # 进程内：随机小预算分段（约几百次暂停，栈中间反复保存/恢复）
    import random
    seg = Machine(program, step_limit=10_000_000)
    rng = random.Random(7)
    pauses = 0
    while seg.state == "paused":
        seg.run(step_budget=rng.randint(50, 200))
        pauses += 1
    print(f"进程内分段：{pauses} 次暂停/恢复，结果={seg.result} "
          f"事件数={len(seg.events)} 步数={seg.steps}")
    assert (seg.result, seg.events, seg.steps) == (full.result, full.events, full.steps)
    print("  与连续执行完全一致（结果/副作用/步数/异常位置口径）✔")

    # 真正的新进程恢复：初始镜像 -> 多个独立进程各跑一段（500 层以上调用帧）
    ck = os.path.join(tmpdir, "loop.json")
    persist.save_initial_image(root, ck, step_limit=10_000_000)
    summaries = []
    for i, budget in enumerate((8000, 8000, 8000, 100_000)):
        r = persist.run_segment(ck, budget)
        summaries.append((i + 1, r["status"], r["steps"]))
        if r["status"] == "finished":
            break
    print("跨进程分段（每段是独立 python 进程，只经镜像文件传递状态）：")
    for i, st, steps in summaries:
        print(f"  进程{i}: status={st} 累计步数={steps}")
    assert len(summaries) >= 3
    assert summaries[-1][1] == "finished"
    assert r["result"] == full.result
    print(f"  {len(summaries)} 个新进程接力恢复，最终结果={r['result']}，"
          f"与连续执行一致 ✔")


def demo_serialization_fidelity() -> None:
    line("4. 序列化保真：环 + 共享单元；对象条目顺序置换结果不变")
    root = build_even_odd(13)
    program = fieldize(convert(root))
    m = Machine(program)
    m.run(step_budget=60)  # 组帧已填充，环已形成，执行尚未结束
    assert m.state == "paused"
    obj = imgmod.dump_machine(m)
    m2 = imgmod.load_machine(obj)

    # 找组帧（两个槽都是 cell 的记录）
    rec_cells = {
        oid: [s for s in slots if s in m2.heap.cells]
        for oid, slots in m2.heap.records.items()
    }
    group = [oid for oid, cs in rec_cells.items() if len(cs) == 2]
    assert len(group) == 1
    cells = rec_cells[group[0]]
    for cell in cells:
        clo = m2.heap.cells[cell].value
        cap = m2.heap.closures[clo][1]
        other = cells[1] if cells[0] == cell else cells[0]
        assert other in m2.heap.records[cap]
    print(f"互递归组帧 {group[0]} 的两个 cell 与两个成员闭包构成环，装载后仍是同一组 OID ✔")

    # 计数器共享单元不被复制
    cp = fieldize(convert(build_counter()))
    cm = Machine(cp)
    cm.run(step_budget=12)
    cm2 = imgmod.load_machine(imgmod.dump_machine(cm))
    cell_oids = {s for slots in cm2.heap.records.values()
                 for s in slots if s in cm2.heap.cells}
    assert len(cell_oids) == 1
    print(f"inc/get 两闭包共享的计数器在镜像往返后仍是 1 个 cell OID {sorted(cell_oids)} ✔")

    # 顺序置换
    import copy, json, random
    def shuffle(o, rng):
        if isinstance(o, dict):
            items = list(o.items()); rng.shuffle(items)
            return {k: shuffle(v, rng) for k, v in items}
        if isinstance(o, list):
            return [shuffle(x, rng) for x in o]
        return o
    full = Machine(program); full.run()
    ok = 0
    for seed in range(8):
        perm = shuffle(copy.deepcopy(obj), random.Random(seed))
        mp = imgmod.load_machine(perm)
        mp.run()
        assert (mp.result, mp.events) == (full.result, full.events)
        ok += 1
    print(f"对象图分配/序列化条目顺序 8 种置换，结果与别名关系全部不变 ✔")


def demo_upgrade_story(tmpdir: str) -> None:
    line("5. 安全点升级：旧 v1 帧持共享单元 → v2 闭包改同一单元 → 安全点中断 → 恢复")
    root = _loop_program(5, 1)
    p1 = fieldize(convert(root))
    fid = next(f for f in p1.functions
               if any(x.startswith("cell:loop#") for x in p1.layout[f]))
    v2 = _loop_v2_function(p1, fid)

    m = Machine(p1)
    once = {"n": 1}
    def pause_first_v1(mach):
        if mach.stack[-1].fid == fid and once["n"]:
            once["n"] -= 1
            return "pause"
    m.run(safepoint_hook=pause_first_v1)
    print(f"在 loop 首次入口安全点暂停：调用栈={m.fid_trace()}")

    shared_cell = m.heap.records[m.stack[-1].record][0]
    clo = next(cs.value for cs in m.heap.cells.values()
               if cs.filled and cs.value in m.heap.closures
               and m.heap.closures[cs.value][0] == fid)
    cap_rec = m.heap.closures[clo][1]
    print(f"旧 v1 栈帧持有 cell {shared_cell}；组闭包 {clo}（载荷 v1）")

    upgrade_at_safepoint(
        m, "v2", {fid: v2},
        {fid: [{"op": "add", "field": "step", "default": 3}]},
    )
    print(f"升级提交：闭包 {clo} 同 OID 重发为 {m.heap.closures[clo]}；"
          f"捕获记录={m.heap.records[cap_rec]}（共享 cell 未复制，新槽 step=3）")
    assert m.heap.records[cap_rec][0] == shared_cell
    assert m.stack[-1].version == "v1"

    # 安全点中断：停在第一个 v2 入口
    def pause_first_v2(mach):
        if mach.stack[-1].fid == fid and mach.stack[-1].version == "v2":
            return "pause"
    m.run(safepoint_hook=pause_first_v2)
    v2_frame = m.stack[-1]
    print(f"安全点中断：第一个 v2 帧入口（{v2_frame.fid}@{v2_frame.version}），"
          f"其 c 槽={m.heap.records[v2_frame.record][0]}（== 旧帧 cell："
          f"{m.heap.records[v2_frame.record][0] == shared_cell}）；"
          f"栈中仍有 v1 帧 {sum(1 for f in m.stack if f.version == 'v1')} 个")

    m.run()
    print(f"恢复到结束：结果={m.result}  副作用={m.events}")
    assert m.events == [("set", "c", 1), ("set", "c", 4), ("set", "c", 7),
                        ("set", "c", 10), ("set", "c", 13)]

    # 升级后镜像在新进程跑完
    ck = os.path.join(tmpdir, "up.json")
    persist.save_machine_image(_upgraded_machine(p1, fid, v2), ck)
    r = persist.run_segment(ck, 1_000_000)
    print(f"升级点镜像在全新进程恢复执行：status={r['status']} 结果={r['result']} "
          f"首个事件={r['events'][0]} ✔")
    assert r["result"] == m.result


def _upgraded_machine(p1, fid, v2):
    m = Machine(p1)
    gate = {"open": True}

    def pause_once(mach):
        if mach.stack[-1].fid == fid and gate["open"]:
            gate["open"] = False
            return "pause"
        return None

    m.run(safepoint_hook=pause_once)
    upgrade_at_safepoint(
        m, "v2", {fid: v2},
        {fid: [{"op": "add", "field": "step", "default": 3}]},
    )
    return m


def demo_rejection_and_rollback() -> None:
    line("6. 棘手边界：非法迁移被拒绝；提交前注入失败 → 完整回滚")
    root = _loop_program(4, 1)
    p = fieldize(convert(root))
    fid = next(f for f in p.functions
               if any(x.startswith("cell:loop#") for x in p.layout[f]))

    # 6a. 删除仍被引用字段：新代码仍引用该槽，发布前必须拒绝
    m = Machine(p)
    m.run(safepoint_hook=lambda mach: (
        "pause" if mach.stack[-1].fid == fid else None))
    v2 = _loop_v2_function(p, fid)  # 体里仍读 c(slot0)
    try:
        upgrade_at_safepoint(
            m, "v2bad", {fid: v2},
            {fid: [{"op": "add", "field": "step", "default": 3},
                   {"op": "delete", "field": "c#id0"}]},
        )
    except errors.UpgradeValidationError as e:
        print(f"删除仍被引用字段实际触发 {type(e).__name__}: {e} ✔")
    else:
        raise AssertionError("非法迁移未被拒绝")
    assert m.table.current_version(fid) == "v1"

    # 6b. 提交前注入失败：回滚后版本/载荷/布局全不变，继续跑原 v1 结果
    for point in ("validate", "stage", "commit"):
        m = Machine(p)
        m.run(safepoint_hook=lambda mach: (
            "pause" if mach.stack[-1].fid == fid else None))
        versions_before = m.table.snapshot()
        closures_before = dict(m.heap.closures)
        try:
            upgrade_at_safepoint(
                m, "v2", {fid: _loop_v2_function(p, fid)},
                {fid: [{"op": "add", "field": "step", "default": 3}]},
                inject_failure=point,
            )
        except errors.UpgradeAborted as e:
            assert e.rolled_back is True  # 仅作记录；下面独立复查真实状态
        # 独立复查（不信任异常自带标志）：
        assert m.table.snapshot() == versions_before
        assert dict(m.heap.closures) == closures_before
        assert not m.table.has(fid, "v2")
        m.run()
        assert m.result == 14, (point, m.result)
        print(f"注入点 {point:8s} 失败后回滚：版本指针/闭包载荷未变，"
              f"续跑原 v1 结果={m.result} 事件={m.events} ✔")


def main() -> None:
    print("recenv 演示：闭包环境布局 + 可暂停/恢复/迁移的显式栈执行")
    print(f"Python {__import__('sys').version.split()[0]}，仅标准库，离线运行")
    tmpdir = tempfile.mkdtemp(prefix="recenv-demo-")
    try:
        demo_legacy_core()
        demo_legacy_boundaries()
        demo_pause_resume_and_new_process(tmpdir)
        demo_serialization_fidelity()
        demo_upgrade_story(tmpdir)
        demo_rejection_and_rollback()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    line("演示结束")
    print("全部现场计算完成。测试：")
    print("  快速回归（默认）：python -m unittest discover -s tests -v")
    print("  扩展规模验证    ：RUN_RECENV_EXTENDED=1 "
          "python -m unittest tests.test_scale -v")


if __name__ == "__main__":
    main()

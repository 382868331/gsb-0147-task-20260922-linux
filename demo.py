"""固定输入演示（确定性、离线、无第三方依赖）。

运行::

    python demo.py                 # 完整演示（< 90 秒，实测秒级）
    python demo.py --resume FILE   # 子进程模式：从快照恢复并跑完（由主演示调用）

内容（全部现场真实计算，无 sleep、无预录答案）：

1. 原有能力：两个闭包共享 cell 计数器；letrec 互递归 even/odd；源/目标解释器对照。
2. **暂停—保存—新进程恢复**：显式栈机器在调用入口安全点暂停，序列化为快照字节，
   交给一个全新的 Python 子进程恢复并跑完；与同一机器连续执行的结果逐项对照。
3. **函数版本表 + 显式字段迁移 + 安全点热升级**：旧版本栈帧持有共享 cell，
   在安全点升级（c→kount 重命名、新增常量 bias 字段），迁移后的新闭包旁路
   再次修改同一个 cell，随后旧栈恢复执行——同一执行例中三种现象同时出现。
4. 棘手边界：版本必须恰好 +1、迁移规则非法（删除仍被引用字段）被拒绝。
5. 真实失败：提交前注入失败（before_rewire）→ MigrationError 且完整回滚，
   机器随后仍按旧版本跑出确定结果。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from recenv import ast_nodes as ast
from recenv import (
    Machine,
    MigrationRules,
    convert,
    errors,
    eval_source,
    eval_target,
    snapshot,
)
from recenv.heap import CellObj, ClosureObj, Ref


def line(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ---------------------------------------------------------------------------
# 程序构造
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
        ast.If0(
            ast.Var("n"), ast.Int(1),
            ast.Call(ast.Var("odd"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    od = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"), ast.Int(0),
            ast.Call(ast.Var("even"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    return ast.LetRec(
        [ast.RecBinding("even", ev), ast.RecBinding("odd", od)],
        ast.Call(ast.Var("even"), [ast.Int(n)]),
    )


def bump_v1(n: int) -> ast.Expr:
    # bump@1(x)：IIFE 入口是安全点；帧在该点已持有共享 cell 但尚未读它
    bump = ast.Lambda(
        ["x"],
        ast.Call(
            ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Var("x")))),
            [],
        ),
        ver="bump@1",
    )
    drive = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"), ast.Int(0),
            ast.Add(
                ast.Call(ast.Var("bump"), [ast.Int(1)]),
                ast.Call(ast.Var("drive"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        ),
    )
    return ast.Let(
        "c", ast.Int(0),
        ast.Let(
            "bump", bump,
            ast.LetRec(
                [ast.RecBinding("drive", drive)],
                ast.Add(ast.Call(ast.Var("drive"), [ast.Int(n)]), ast.Var("c")),
            ),
        ),
    )


def bump_v2(n: int, bias: int) -> ast.Expr:
    # 字段迁移：c 重命名为 kount；新增常量字段 bias
    bump = ast.Lambda(
        ["x"],
        ast.Set(
            "kount",
            ast.Add(ast.Add(ast.Var("kount"), ast.Var("x")), ast.Var("bias")),
        ),
        ver="bump@2",
    )
    drive = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"), ast.Int(0),
            ast.Add(
                ast.Call(ast.Var("bump"), [ast.Int(1)]),
                ast.Call(ast.Var("drive"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        ),
    )
    return ast.Let(
        "bias", ast.Int(bias),
        ast.Let(
            "kount", ast.Int(0),
            ast.Let(
                "bump", bump,
                ast.LetRec(
                    [ast.RecBinding("drive", drive)],
                    ast.Add(
                        ast.Call(ast.Var("drive"), [ast.Int(n)]),
                        ast.Var("kount"),
                    ),
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 演示段
# ---------------------------------------------------------------------------

def demo_legacy_equivalence() -> None:
    line("1) 原有能力保持：共享 cell 计数器 + 互递归 even/odd（源/目标对照）")
    root = build_counter()
    s_value, s_events = eval_source(root)
    program = convert(root)
    t_value, t_events = eval_target(program)
    m_value, m_events = Machine(program).run_to_completion()
    print(f"计数器：源={s_value} 目标IR={t_value} 显式栈机器={m_value}，事件={m_events}")
    assert (s_value, s_events) == (t_value, t_events) == (m_value, m_events)

    n = 9
    root2 = build_even_odd(n)
    sv, _ = eval_source(root2)
    tv, _ = eval_target(convert(root2))
    mv, _ = Machine(convert(root2)).run_to_completion()
    print(f"互递归 even({n})：源={sv} 目标IR={tv} 显式栈机器={mv}")
    assert sv == tv == mv == 0
    print("三种执行方式结果完全一致 ✔")


def demo_cross_process_resume() -> None:
    line("2) 暂停 → 保存快照 → 全新进程恢复 → 结果与连续执行一致")
    program = convert(bump_v1(4))

    # 机器 A：分段执行，直到安全点上已有多个在途栈帧（深递归中段）才暂停
    machine_a = Machine(program, step_limit=10_000)
    while True:
        status = machine_a.run_segment(3)
        if status == "paused" and len(machine_a.call_trace()) >= 4:
            break
        if status == "done":
            raise AssertionError("程序在到达深栈暂停点前结束")
    print(f"机器 A 在调用入口安全点暂停：已用步数 {machine_a.steps}，"
          f"在途调用栈深 {len(machine_a.call_trace())}（钉住各自代码版本）")

    blob = snapshot.dumps(machine_a)
    print(f"序列化为快照：{len(blob.encode('utf-8'))} 字节 JSON"
          f"（含引用图、代码哈希、续体栈）")

    # 机器 B：同一程序连续执行（独立参考，不接触快照）
    value_cont, events_cont = Machine(program, step_limit=10_000).run_to_completion()

    # 机器 C：全新的操作系统进程，仅从快照字节恢复后跑完
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(blob)
        snap_path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--resume", snap_path],
            capture_output=True, text=True, check=False,
        )
    finally:
        os.unlink(snap_path)
    if proc.returncode != 0:
        raise AssertionError(f"恢复子进程失败:\n{proc.stderr}")
    payload = json.loads(proc.stdout)
    print(f"新进程(pid 由系统分配)恢复后跑完：结果={payload['value']}，"
          f"事件={payload['events']}")
    print(f"同程序连续执行（独立对照）：结果={value_cont}，事件={events_cont}")
    assert payload["value"] == value_cont
    assert [tuple(e) for e in payload["events"]] == events_cont
    print("跨进程暂停/恢复与连续执行的可见结果、事件序列完全一致 ✔")


def demo_upgrade_shared_cell() -> None:
    line("3) 安全点热升级：旧栈帧持共享 cell → 迁移 → 新闭包再写该 cell → 旧栈续跑")
    p1 = convert(bump_v1(3))
    p2 = convert(bump_v2(2, bias=100))
    m = Machine(p1)

    # 暂停到 bump@1 帧已在栈上的安全点
    m.start()
    for _ in range(400):
        m.run_segment(1)
        if any(u == "bump" and v == 1 for _, u, v in m.call_trace()):
            break
    old_closure = [
        m.heap.get(Ref(i)) for i in m.heap.all_oids()
        if isinstance(m.heap.get(Ref(i)), ClosureObj)
        and m.bundle.get(m.heap.get(Ref(i)).fkey).unit == "bump"
    ][0]
    shared_cell = m.heap.record(old_closure.record).slots[0]
    print(f"暂停点：bump@1 旧栈帧在栈上；共享 cell #{shared_cell.oid} "
          f"= {m.heap.cell(shared_cell).value}")

    m.request_upgrade(
        "bump", p2,
        MigrationRules(rename={"c": "kount"}, add={"bias": 100}),
    )
    handles = m.apply_pending_upgrade()
    print("版本表升级 bump@1 → bump@2；字段规则：rename c→kount，add bias=100")
    new_closure = [
        m.heap.get(Ref(i)) for i in m.heap.all_oids()
        if isinstance(m.heap.get(Ref(i)), ClosureObj)
        and m.bundle.get(m.heap.get(Ref(i)).fkey).unit == "bump"
        and m.bundle.get(m.heap.get(Ref(i)).fkey).version == 2
    ][0]
    entry = m.bundle.get(new_closure.fkey)
    k_slot = m.heap.record(new_closure.record).slots[
        list(entry.free_names).index("kount")
    ]
    print(f"迁移后新闭包的 kount 槽 = #{k_slot.oid}（必须等于旧 cell "
          f"#{shared_cell.oid}：{'是，别名保留' if k_slot == shared_cell else '否！'}）")
    assert k_slot == shared_cell

    # 同一执行例：旧栈仍暂停时旁路运行一次迁移后的新闭包
    m.invoke(handles[0]["$self"], [1], side=True)
    side_value, side_events = m.run_side()
    print(f"旁路调用迁移后的 bump@2(1)：返回 {side_value}，事件 {side_events}；"
          f"共享 cell 现为 {m.heap.cell(shared_cell).value}")
    assert side_value == 101

    value, events = m.run_to_completion()
    print(f"旧栈恢复执行后最终结果 {value}，完整事件序列：")
    for e in events:
        print("   ", e)
    # 手工小参考：旁路 101；旧 bump@1 在安全点后读到 101 → 102；
    # 两次 bump@2：203、304；drive 和 609 + 末值 304 = 913
    assert value == 913
    assert events == [
        ("set", "kount", 101), ("set", "c", 102),
        ("set", "kount", 203), ("set", "kount", 304),
    ]
    print("旧版本帧旧代码、新版本新调用、共享 cell 身份三者同时成立 ✔")


def demo_rule_rejection() -> None:
    line("4) 棘手边界：非法迁移规则 / 版本跳跃 必须被拒绝")
    p1 = convert(bump_v1(2))
    p2 = convert(bump_v2(2, bias=100))

    # 4a) 不给 add(bias)：字段集无法闭合
    m = Machine(p1)
    m.start()
    for _ in range(400):
        m.run_segment(1)
        if any(u == "bump" for _, u, _ in m.call_trace()):
            break
    m.request_upgrade("bump", p2, MigrationRules(rename={"c": "kount"}))
    try:
        m.apply_pending_upgrade()
    except errors.MigrationError as e:
        print(f"缺少 add 规则被拒绝：{type(e).__name__}: {e}")
    else:
        raise AssertionError("非法迁移未被拒绝")
    assert m.status == "paused" and m.applied_upgrades == []

    # 4b) add 默认值必须是常量 int
    try:
        MigrationRules(add={"bias": 1.5})
    except errors.MigrationError as e:
        print(f"非常量默认值被拒绝：{type(e).__name__}: {e}")

    # 4c) 版本必须恰好 +1：用 bump@3 直接接 @1（版本表查不到 @2）
    def bump_v3() -> ast.Expr:
        b = ast.Lambda(
            ["x"],
            ast.Set("kount",
                    ast.Add(ast.Add(ast.Var("kount"), ast.Var("x")),
                            ast.Var("bias"))),
            ver="bump@3",
        )
        d = ast.Lambda(
            ["n"],
            ast.If0(ast.Var("n"), ast.Int(0),
                    ast.Add(ast.Call(ast.Var("bump"), [ast.Int(1)]),
                            ast.Call(ast.Var("drive"),
                                     [ast.Add(ast.Var("n"), ast.Int(-1))]))),
        )
        return ast.Let(
            "bias", ast.Int(1),
            ast.Let("kount", ast.Int(0),
            ast.Let("bump", b,
            ast.LetRec([ast.RecBinding("drive", d)],
                       ast.Add(ast.Call(ast.Var("drive"), [ast.Int(1)]),
                               ast.Var("kount"))))))

    m2 = Machine(convert(bump_v1(2)))
    m2.start()
    for _ in range(400):
        m2.run_segment(1)
        if any(u == "bump" for _, u, _ in m2.call_trace()):
            break
    m2.request_upgrade("bump", convert(bump_v3()), MigrationRules())
    try:
        m2.apply_pending_upgrade()
    except errors.RecenvError as e:
        print(f"版本跳跃 @1→@3 被拒绝：{type(e).__name__}: {e}")
    else:
        raise AssertionError("版本跳跃未被拒绝")
    print("所有非法升级在发布前被拒绝，机器不产生部分新环境 ✔")


def demo_injected_rollback() -> None:
    line("5) 真实失败：提交前注入失败 → 完整回滚，旧版本照常跑完")
    p1 = convert(bump_v1(3))
    p2 = convert(bump_v2(3, bias=100))
    m = Machine(p1)
    v_before, ev_before = Machine(p1).run_to_completion()

    m.start()
    for _ in range(400):
        m.run_segment(1)
        if any(u == "bump" and v == 1 for _, u, v in m.call_trace()):
            break
    heap_ids_before = set(m.heap.all_oids())
    units_before = m.bundle.versions("bump")
    m.request_upgrade(
        "bump", p2,
        MigrationRules(rename={"c": "kount"}, add={"bias": 100}),
        failpoint="before_rewire",
    )
    try:
        m.apply_pending_upgrade()
    except errors.MigrationError as e:
        print(f"注入失败触发：{e}")
    else:
        raise AssertionError("注入失败未触发")
    assert set(m.heap.all_oids()) == heap_ids_before, "回滚后堆对象集合必须不变"
    assert m.bundle.versions("bump") == units_before == [1]
    assert m.applied_upgrades == []
    print("回滚后：堆对象集合、版本表、已应用升级记录全部回到升级前 ✔")

    value, events = m.run_to_completion()
    print(f"机器继续按 bump@1 跑完：结果 {value}，事件 {events}")
    assert (value, events) == (v_before, ev_before)
    print("回滚后的可见结果与从未升级的独立运行完全一致 ✔")


def resume_worker(path: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        m = snapshot.loads(f.read())
    value, events = m.run_to_completion()
    sys.stdout.write(json.dumps({"value": value, "events": events}))


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--resume":
        resume_worker(sys.argv[2])
        return 0
    print("recenv 演示：闭包暂停/保存/跨进程恢复 + 版本化迁移 + 安全点升级")
    print(f"Python {sys.version.split()[0]}，仅标准库，离线运行")
    demo_legacy_equivalence()
    demo_cross_process_resume()
    demo_upgrade_shared_cell()
    demo_rule_rejection()
    demo_injected_rollback()
    line("演示结束")
    print("快速回归：python -m unittest discover -s tests -v")
    print("扩展验证：python -m unittest discover -s tests_ext -v")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

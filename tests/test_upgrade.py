"""函数版本表与显式字段迁移测试。

核心场景（同一执行例内全部出现）：

* **旧版本栈帧持有共享单元**：升级发生在调用入口安全点，栈顶 v1 激活帧
  捕获的计数器 cell 与 letrec 组闭包是同一 OID；
* **迁移后的新闭包再次修改该单元**：v2 闭包载荷在同一 OID 上重发，
  捕获记录按字段计划迁移（删/改名/增默认字段），共享 cell OID 不变，
  v2 递归调用对同一 cell 写入；
* **安全点中断**：升级后继续执行，在第一个 v2 激活入口再次暂停，
  再恢复到结束；
* **互递归组**整体升级（两个 fid 一次原子提交）；
* **迁移失败回滚**：validate/stage/commit 三个注入点失败后，版本指针、
  闭包载荷版本、堆记录布局、栈帧版本全部不变，机器仍能跑出原 v1 结果；
* 非法迁移（删除仍被引用字段、新增非常量默认、改参数表、坐标与计划不符）
  在提交前被拒绝。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace

from recenv import ast_nodes as A
from recenv import convert, errors
from recenv import image as imgmod
from recenv import persist
from recenv.machine import Machine
from recenv.migration import fieldize, upgrade_at_safepoint


def _loop_src(n=5, inc=1):
    """let c=0 in letrec loop(n)= if0(n,c, set(c,c+inc)+loop(n-1)) in loop(n)."""
    body = A.If0(
        A.Var("n"), A.Var("c"),
        A.Add(A.Set("c", A.Add(A.Var("c"), A.Int(inc))),
              A.Call(A.Var("loop"), [A.Add(A.Var("n"), A.Int(-1))])),
    )
    root = A.Let(
        "c", A.Int(0),
        A.LetRec([A.RecBinding("loop", A.Lambda(["n"], body))],
                 A.Call(A.Var("loop"), [A.Int(n)])),
    )
    return root


def _cap_fields(p, fid, version="v1"):
    """函数某版本捕获槽的结构化字段名（限定名）列表。"""
    sid = p.function_field_map[fid][version]
    fn = p.functions[fid]
    return [fs.field_name for fs in p.frame_schemas[sid][:fn.n_capture]]


def _pause_first_entry(m, fid, version=None, once=True):
    state = {"left": 1}
    def hook(mach):
        top = mach.stack[-1]
        if top.fid == fid and (version is None or top.version == version):
            if once:
                if state["left"]:
                    state["left"] -= 1
                    return "pause"
            else:
                return "pause"
    return hook


class TestUpgradeStory(unittest.TestCase):
    def setUp(self):
        self.p1 = fieldize(convert(_loop_src(5, 1)))
        self.fid = next(
            f for f in self.p1.functions
            if any(x.startswith("cell:loop#") for x in self.p1.layout[f])
        )
        self.fn1 = self.p1.functions[self.fid]

    def _v2_function(self, bump_const_field_slot=2):
        # v1 布局：slot0=c cell, slot1=loop rec cell, slot2=n(param,val)
        # v2 计划：新增字段 step(默认3)  -> slot2=step val, slot3=n param
        # v2 体： if0(Get 3, CellGet 0,
        #             add(CellSet(0, add(CellGet 0, Get 2)),
        #                 call(CellGet 1, [add(Get 3, -1)])))
        body = A.TIf0(
            A.TGet(0, 3),
            A.TCellGet(0, 0, "c"),
            A.TPrim("add", (
                A.TCellSet(0, 0,
                           A.TPrim("add", (A.TCellGet(0, 0, "c"), A.TGet(0, 2))),
                           "c"),
                A.TCall(A.TCellGet(0, 1, "loop"),
                        (A.TPrim("add", (A.TGet(0, 3), A.TConst(-1))),)),
            )),
        )
        # 新 free_names 约定为新捕获字段限定名（与字段计划逐槽一致）
        sid = self.p1.function_field_map[self.fid]["v1"]
        cap_names = [fs.field_name for fs in self.p1.frame_schemas[sid]][:self.fn1.n_capture]
        return replace(self.fn1, n_capture=3, n_slots=4, body=body,
                       free_names=tuple(cap_names) + ("step",))

    def test_old_frame_shared_cell_new_closures_mutate_safepoint_interrupt(self):
        m = Machine(self.p1)
        m.run(safepoint_hook=_pause_first_entry(m, self.fid))
        self.assertTrue(m.at_safepoint)
        self.assertEqual(m.fid_trace(), [(self.fid, "v1")])

        old_act = m.stack[-1].record
        shared_cell = m.heap.records[old_act][0]
        # 组闭包（v1 载荷）及其捕获记录
        clo_oid = next(
            cs.value for cs in m.heap.cells.values()
            if cs.filled and cs.value in m.heap.closures
            and m.heap.closures[cs.value][0] == self.fid
        )
        cap_rec = m.heap.closures[clo_oid][1]
        self.assertEqual(m.heap.closures[clo_oid][2], "v1")
        self.assertEqual(m.heap.records[cap_rec][0], shared_cell)

        newfn = self._v2_function()
        upgrade_at_safepoint(
            m, "v2", {self.fid: newfn},
            {self.fid: [{"op": "add", "field": "step", "default": 3}]},
        )

        # 旧栈帧仍是 v1；闭包载荷在同 OID 升级到 v2；捕获记录保留 cell OID
        self.assertEqual(m.stack[-1].version, "v1")
        self.assertEqual(m.heap.closures[clo_oid], (self.fid, cap_rec, "v2"))
        new_slots = m.heap.records[cap_rec]
        self.assertEqual(new_slots[0], shared_cell)       # 共享单元没复制
        self.assertEqual(new_slots[2], 3)                 # 常量默认值
        self.assertEqual(m.heap.rec_schema[cap_rec], f"capture:{self.fid}:v2")
        # 旧激活帧记录仍是 v1 布局（3 槽）
        self.assertEqual(m.heap.rec_schema[old_act], f"act:{self.fid}:v1")
        self.assertEqual(len(m.heap.records[old_act]), 3)

        # 安全点中断：在第一个 v2 入口暂停
        m.run(safepoint_hook=_pause_first_entry(m, self.fid, version="v2"))
        self.assertTrue(m.at_safepoint)
        v2_frame = m.stack[-1]
        self.assertEqual(v2_frame.version, "v2")
        self.assertEqual(len(m.heap.records[v2_frame.record]), 4)  # 新布局
        # 旧 v1 帧仍在栈中，且仍持同一 cell
        v1_frames = [f for f in m.stack if f.version == "v1"]
        self.assertTrue(v1_frames)
        self.assertEqual(m.heap.records[v1_frames[-1].record][0], shared_cell)

        # v2 帧的 c 槽也是同一个 cell
        self.assertEqual(m.heap.records[v2_frame.record][0], shared_cell)

        # 恢复到结束
        m.run()
        # v1 顶层帧 set c=1；v2(4..1) 每次 +3：
        # v2(0)=c=13；v2(1)=26；v2(2)=36；v2(3)=43；v2(4)=47；f(5)=1+47=48
        self.assertEqual(
            m.events,
            [("set", "c", 1), ("set", "c", 4), ("set", "c", 7),
             ("set", "c", 10), ("set", "c", 13)],
        )
        self.assertEqual(m.result, 48)

    def test_upgraded_image_resumes_in_new_process(self):
        m = Machine(self.p1)
        m.run(safepoint_hook=_pause_first_entry(m, self.fid))
        upgrade_at_safepoint(
            m, "v2", {self.fid: self._v2_function()},
            {self.fid: [{"op": "add", "field": "step", "default": 3}]},
        )
        tmp = tempfile.mkdtemp(prefix="recenv-up-")
        try:
            ck = os.path.join(tmp, "up.json")
            persist.save_machine_image(m, ck)
            r = persist.run_segment(ck, 1_000_000)
            self.assertEqual(r["status"], "finished")
            self.assertEqual(r["result"], 48)
            self.assertEqual(r["events"][0], ("set", "c", 1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRenameDeleteAdd(unittest.TestCase):
    def _program(self):
        # loop 捕获两个外层不可变计数器：a, b；v2 计划 rename a -> aa,
        # delete b, add d=7
        body = A.If0(
            A.Var("n"), A.Var("a"),
            A.Add(A.Var("a"),
                  A.Add(A.Var("b"),
                        A.Call(A.Var("loop"),
                               [A.Add(A.Var("n"), A.Int(-1))]))),
        )
        root = A.Let(
            "a", A.Int(100),
            A.Let("b", A.Int(7),
                  A.LetRec([A.RecBinding("loop", A.Lambda(["n"], body))],
                           A.Call(A.Var("loop"), [A.Int(2)]))),
        )
        return fieldize(convert(root))

    def test_rename_delete_add_accepted(self):
        p = self._program()
        fid = next(f for f in p.functions if "loop#" in " ".join(p.layout[f]))
        fn = p.functions[fid]
        sid = p.function_field_map[fid]["v1"]
        names = [fs.field_name for fs in p.frame_schemas[sid]]
        # v1 捕获槽: a#id?, b#id?, loop#id? ; 参数 n
        a_name = next(x for x in names if x.startswith("a#"))
        b_name = next(x for x in names if x.startswith("b#"))
        loop_name = next(x for x in names if x.startswith("loop#"))
        n_name = next(x for x in names if x.startswith("param n"))

        # v2 新布局槽：0=aa(原 a), 1=loop, 2=d(默认7), 3=n 参数
        body2 = A.TIf0(
            A.TGet(0, 3),
            A.TGet(0, 0),
            A.TPrim("add", (
                A.TGet(0, 0),
                A.TCall(A.TCellGet(0, 1, "loop"),
                        (A.TPrim("add", (A.TGet(0, 3), A.TConst(-1))),)),
            )),
        )
        newfn = replace(fn, n_capture=3, n_slots=4, body=body2,
                        free_names=("aa", loop_name, "d"))
        m = Machine(p)
        m.run(safepoint_hook=_pause_first_entry(m, fid))
        upgrade_at_safepoint(
            m, "v2", {fid: newfn},
            {fid: [
                {"op": "rename", "old": a_name, "new": "aa"},
                {"op": "delete", "field": b_name},
                {"op": "add", "field": "d", "default": 7},
            ]},
        )
        m.run()
        # 旧 v1 顶层帧仍读 b：100+7+loop(1)；v2 loop(1)=100+loop(0)=200；307
        self.assertEqual(m.result, 307)


class TestMutualRecursionUpgrade(unittest.TestCase):
    def test_upgrade_both_members_atomically_shared_counter(self):
        # v1: even/odd 互递归，两成员都捕获外层可变计数器 c 并在递归分支
        #     set(c,c+1)；v2: 增量改读新字段 step(默认常量 5)。
        # 升级在 even(4) 的首次入口安全点一次性原子提交两个 fid：
        # 旧 even v1 帧持有的 c cell 与迁移后 v2 组闭包是同一 OID。
        ev1 = A.Lambda(["n"], A.If0(
            A.Var("n"), A.Int(1),
            A.Add(A.Set("c", A.Add(A.Var("c"), A.Int(1))),
                  A.Call(A.Var("odd"), [A.Add(A.Var("n"), A.Int(-1))]))))
        od1 = A.Lambda(["n"], A.If0(
            A.Var("n"), A.Int(0),
            A.Add(A.Set("c", A.Add(A.Var("c"), A.Int(1))),
                  A.Call(A.Var("even"), [A.Add(A.Var("n"), A.Int(-1))]))))
        root = A.Let("c", A.Int(0),
            A.LetRec([A.RecBinding("even", ev1), A.RecBinding("odd", od1)],
                     A.Call(A.Var("even"), [A.Int(4)])))
        p = fieldize(convert(root))

        # 函数布局只列出*捕获*槽：even 捕获 odd，故含 "odd#" 的才是 even
        fids = {}
        for fid in p.functions:
            labs = " ".join(p.layout[fid])
            if "odd#" in labs:
                fids["even"] = fid
            elif "even#" in labs:
                fids["odd"] = fid
        self.assertEqual(set(fids), {"even", "odd"})

        # v2 IR（新布局槽：0=c cell，1=对方成员 rec cell，2=step val，3=n 参数）
        def v2_body(base_const, other_name):
            return A.TIf0(
                A.TGet(0, 3),
                A.TConst(base_const),
                A.TPrim("add", (
                    A.TCellSet(
                        0, 0,
                        A.TPrim("add", (A.TCellGet(0, 0, "c"), A.TGet(0, 2))),
                        "c",
                    ),
                    A.TCall(
                        A.TCellGet(0, 1, other_name),
                        (A.TPrim("add", (A.TGet(0, 3), A.TConst(-1))),),
                    ),
                )),
            )

        new_code, ops = {}, {}
        for who, other, base in (("even", "odd", 1), ("odd", "even", 0)):
            fid = fids[who]
            fn = p.functions[fid]
            # 旧捕获槽的限定字段名（按绑定 ID）；c 一定是第一个
            sid = p.function_field_map[fid]["v1"]
            old_free_q = [fs.field_name
                          for fs in p.frame_schemas[sid][:fn.n_capture]]
            self.assertTrue(old_free_q[0].startswith("c#"))
            new_free = tuple(old_free_q + ["step"])
            new_code[fid] = replace(
                fn, n_capture=3, n_slots=4,
                body=v2_body(base, other), free_names=new_free,
            )
            ops[fid] = [{"op": "add", "field": "step", "default": 5}]

        m = Machine(p)
        m.run(safepoint_hook=_pause_first_entry(m, fids["even"]))
        self.assertEqual(m.fid_trace(), [(fids["even"], "v1")])

        # 旧 v1 帧持有的 c cell OID
        old_cell = m.heap.records[m.stack[-1].record][0]
        # 两个组闭包升级前的捕获记录
        cap_recs_before = {
            fid: m.heap.closures[
                next(cs.value for cs in m.heap.cells.values()
                     if cs.filled and cs.value in m.heap.closures
                     and m.heap.closures[cs.value][0] == fid)
            ][1]
            for fid in fids.values()
        }
        self.assertEqual(m.heap.records[cap_recs_before[fids["even"]]][0], old_cell)
        self.assertEqual(m.heap.records[cap_recs_before[fids["odd"]]][0], old_cell)

        upgrade_at_safepoint(m, "v2", new_code, ops)

        # 两闭包同 OID 升 v2；两捕获记录仍是同一 c cell，且新槽 step=5
        for fid in fids.values():
            clo = next(cs.value for cs in m.heap.cells.values()
                       if cs.filled and cs.value in m.heap.closures
                       and m.heap.closures[cs.value][0] == fid)
            self.assertEqual(m.heap.closures[clo], (fid, cap_recs_before[fid], "v2"))
            slots = m.heap.records[cap_recs_before[fid]]
            self.assertEqual(slots[0], old_cell)   # 共享 cell 未复制
            self.assertEqual(slots[2], 5)

        # 旧 even v1 帧继续旧版本，新调用走 v2
        m.run()
        self.assertEqual([e[2] for e in m.events], [1, 6, 11, 16])
        # even4(v1):1 + odd3(v2):6+even2:11+odd1:16+even0(base 1) = 35
        self.assertEqual(m.result, 35)


class TestVersionViewFreeze(unittest.TestCase):
    def test_old_frame_builds_v1_closure_new_frame_builds_v2(self):
        # v1: let c=0 in letrec g(n)= if0(n,0,
        #        let h = lambda() set(c,c+10) in h()+g(n-1)) in g(3)
        # 升级（首次 g 入口安全点）：g 与 h 同发 v2；h v2 新增常量字段
        # step=5（捕获槽 1->2），g v2 体中构造 h 的 TMkClosure 改为抽取
        # 2 个环境槽（cell + 常量 5），h v2 体 c += 槽1(step)。
        #
        # 关键：升级点栈上的旧 g@v1 帧恢复后，其冻结版本视图把 h 解析为
        # v1（1 槽）；递归进入的新 g@v2 帧把 h 解析为 v2（2 槽）。
        # 若按“全局 current”解析，旧帧构造 h 时会按 v2 抽 2 槽而 IR 只有
        # 1 个 env，直接崩溃/错配。
        import copy as _copy
        h_src = A.Lambda([], A.Set("c", A.Add(A.Var("c"), A.Int(10))))
        g_body = A.If0(
            A.Var("n"), A.Int(0),
            A.Let(
                "h", h_src,
                A.Add(A.Call(A.Var("h"), []),
                      A.Call(A.Var("g"), [A.Add(A.Var("n"), A.Int(-1))])),
            ),
        )
        root = A.Let(
            "c", A.Int(0),
            A.LetRec([A.RecBinding("g", A.Lambda(["n"], g_body))],
                     A.Call(A.Var("g"), [A.Int(3)])),
        )
        p = fieldize(convert(root))
        fid_g = next(f for f in p.functions
                     if any(x.startswith("cell:g#") for x in p.layout[f]))
        fid_h = next(f for f in p.functions if f != fid_g)
        self.assertEqual(p.functions[fid_h].n_capture, 1)  # h 只捕获 c

        # g v2 体：深拷贝 g v1 体，把其中构造 h 的 TMkClosure 加一个常量槽
        def patch_g(node):
            if isinstance(node, A.TMkClosure) and node.fid == fid_h:
                return A.TMkClosure(fid_h, node.env + (A.TConst(5),))
            return None
        new_g_body = _rewrite_ir(p.functions[fid_g].body, patch_g)
        g_fields = _cap_fields(p, fid_g)
        new_g = replace(p.functions[fid_g], body=new_g_body,
                        free_names=tuple(g_fields))

        # h v2 体：c += 新字段 step(捕获槽 1)
        h2_body = A.TCellSet(
            0, 0,
            A.TPrim("add", (A.TCellGet(0, 0, "c"), A.TGet(0, 1))),
            "c",
        )
        h_fields = _cap_fields(p, fid_h)
        new_h = replace(p.functions[fid_h], n_capture=2, n_slots=2,
                        body=h2_body, free_names=tuple(h_fields) + ("step",))

        # v1 连续执行的期望（独立源解释器）
        from recenv import eval_source
        sv, se = eval_source(root)
        self.assertEqual(sv, 60)  # g1=30；g2=20+30=50；g3=10+50=60

        m = Machine(p, step_limit=100_000)
        gate = {"open": True}
        def hook(mach):
            if mach.stack[-1].fid == fid_g and gate["open"]:
                gate["open"] = False
                return "pause"
        m.run(safepoint_hook=hook)
        self.assertEqual(m.fid_trace(), [(fid_g, "v1")])
        upgrade_at_safepoint(
            m, "v2",
            {fid_g: new_g, fid_h: new_h},
            {fid_h: [{"op": "add", "field": "step", "default": 5}],
             fid_g: []},
        )
        m.run()
        # g3 旧 v1：h 加 10 -> c=10；g2 v2：+5 -> 15；g1 v2：+5 -> 20
        self.assertEqual(m.events, [("set", "c", 10), ("set", "c", 15),
                                    ("set", "c", 20)])
        # g1=20；g2=15+20=35；g3=10+35=45
        self.assertEqual(m.result, 45)

        # 堆上同时存在 h@v1（旧帧构造）与 h@v2（新帧构造）的闭包，
        # 捕获记录槽数分别为 1 和 2，且第 0 槽是同一个 c cell OID
        by_ver: dict[str, list] = {}
        for cfid, rec_oid, ver in m.heap.closures.values():
            if cfid == fid_h:
                by_ver.setdefault(ver, []).append(m.heap.records[rec_oid])
        self.assertEqual(sorted(by_ver), ["v1", "v2"])
        self.assertTrue(all(len(r) == 1 for r in by_ver["v1"]))
        self.assertTrue(all(len(r) == 2 for r in by_ver["v2"]))
        self.assertTrue(all(r[1] == 5 for r in by_ver["v2"]))
        v1_cells = {r[0] for r in by_ver["v1"]}
        v2_cells = {r[0] for r in by_ver["v2"]}
        self.assertTrue(v1_cells and v1_cells == v2_cells)


def _rewrite_ir(node, patch):
    replacement = patch(node)
    if replacement is not None:
        return replacement
    import dataclasses as _dc
    if isinstance(node, A.TCellSet):
        return _dc.replace(node, value=_rewrite_ir(node.value, patch))
    if isinstance(node, A.TPrim):
        return _dc.replace(node, args=tuple(_rewrite_ir(a, patch) for a in node.args))
    if isinstance(node, A.TMkClosure):
        return _dc.replace(node, env=tuple(_rewrite_ir(x, patch) for x in node.env))
    if isinstance(node, A.TMkCell):
        return _dc.replace(node,
                           value=None if node.value is None
                           else _rewrite_ir(node.value, patch))
    if isinstance(node, A.TFillCell):
        return _dc.replace(node, value=_rewrite_ir(node.value, patch))
    if isinstance(node, A.TCall):
        return _dc.replace(node, func=_rewrite_ir(node.func, patch),
                           args=tuple(_rewrite_ir(a, patch) for a in node.args))
    if isinstance(node, A.TIf0):
        return _dc.replace(node, cond=_rewrite_ir(node.cond, patch),
                           then_e=_rewrite_ir(node.then_e, patch),
                           else_e=_rewrite_ir(node.else_e, patch))
    if isinstance(node, A.TWithFrame):
        return _dc.replace(
            node,
            inits=tuple(_rewrite_ir(x, patch) for x in node.inits),
            body=_rewrite_ir(node.body, patch),
        )
    if isinstance(node, A.TBegin):
        return _dc.replace(node, exprs=tuple(_rewrite_ir(x, patch) for x in node.exprs))
    return node


class TestInvalidMigrationsRejected(unittest.TestCase):
    def setUp(self):
        self.p = fieldize(convert(_loop_src(3, 1)))
        self.fid = next(
            f for f in self.p.functions
            if any(x.startswith("cell:loop#") for x in self.p.layout[f])
        )
        self.fn = self.p.functions[self.fid]
        self.m = Machine(self.p)
        self.m.run(safepoint_hook=_pause_first_entry(self.m, self.fid))

    def _v2(self, n_capture=3, body=None, n_slots=4):
        if body is None:
            body = A.TIf0(
                A.TGet(0, 3), A.TCellGet(0, 0, "c"),
                A.TPrim("add", (
                    A.TCellSet(0, 0, A.TCellGet(0, 0, "c"), "c"),
                    A.TCall(A.TCellGet(0, 1, "loop"),
                            (A.TPrim("add", (A.TGet(0, 3), A.TConst(-1))),)),
                )),
            )
        return replace(self.fn, n_capture=n_capture, n_slots=n_slots, body=body,
                       free_names=tuple(_cap_fields(self.p, self.fid)) + ("step",))

    def test_add_requires_constant_default(self):
        with self.assertRaises(errors.UpgradeValidationError):
            upgrade_at_safepoint(
                self.m, "v2", {self.fid: self._v2()},
                {self.fid: [{"op": "add", "field": "step"}]},  # 无 default
            )

    def test_delete_referenced_field_rejected(self):
        # v2 仍引用 c（slot0），却声称删除它：free_names 与计划不符
        v2 = replace(self._v2(), n_capture=2, n_slots=3,
                     free_names=self.fn.free_names)  # 仍含 c
        with self.assertRaises(errors.UpgradeValidationError):
            upgrade_at_safepoint(
                self.m, "v2b", {self.fid: v2},
                {self.fid: [{"op": "add", "field": "step", "default": 3},
                            {"op": "delete", "field": "c#id0"}]},
            )

    def test_param_list_cannot_change(self):
        v2 = replace(self._v2(), params=("m",))
        with self.assertRaises(errors.UpgradeValidationError):
            upgrade_at_safepoint(
                self.m, "v2", {self.fid: v2},
                {self.fid: [{"op": "add", "field": "step", "default": 3}]},
            )

    def test_rename_parameter_rejected(self):
        with self.assertRaises(errors.UpgradeValidationError):
            upgrade_at_safepoint(
                self.m, "v2", {self.fid: self._v2()},
                {self.fid: [{"op": "add", "field": "step", "default": 3},
                            {"op": "rename", "old": "param n#id2", "new": "mm"}]},
            )

    def test_capture_count_mismatch_rejected(self):
        # 没有任何字段操作但新代码 n_capture 变了
        v2 = self._v2()
        with self.assertRaises(errors.UpgradeValidationError):
            upgrade_at_safepoint(self.m, "v2", {self.fid: v2}, {self.fid: []})

    def test_upgrade_outside_safepoint_rejected(self):
        m2 = Machine(self.p)
        m2.run(step_budget=10)  # 表达式归约中途，非调用入口
        self.assertFalse(m2.at_safepoint)
        with self.assertRaises(errors.MachineStateError):
            upgrade_at_safepoint(
                m2, "v2", {self.fid: self._v2()},
                {self.fid: [{"op": "add", "field": "step", "default": 3}]},
            )

    def test_cannot_add_brand_new_function(self):
        with self.assertRaises(errors.UpgradeValidationError):
            upgrade_at_safepoint(
                self.m, "v2", {"nope": self._v2()},
                {"nope": []},
            )


class TestRollback(unittest.TestCase):
    def setUp(self):
        self.p = fieldize(convert(_loop_src(4, 1)))
        self.fid = next(
            f for f in self.p.functions
            if any(x.startswith("cell:loop#") for x in self.p.layout[f])
        )
        self.fn = self.p.functions[self.fid]
        self.v2 = replace(
            self.fn, n_capture=3, n_slots=4,
            body=A.TIf0(
                A.TGet(0, 3), A.TCellGet(0, 0, "c"),
                A.TPrim("add", (
                    A.TCellSet(0, 0,
                               A.TPrim("add", (A.TCellGet(0, 0, "c"), A.TGet(0, 2))),
                               "c"),
                    A.TCall(A.TCellGet(0, 1, "loop"),
                            (A.TPrim("add", (A.TGet(0, 3), A.TConst(-1))),)),
                )),
            ),
            free_names=tuple(_cap_fields(self.p, self.fid)) + ("step",),
        )
        self.ops = {self.fid: [{"op": "add", "field": "step", "default": 3}]}

    def _paused_machine(self):
        m = Machine(self.p)
        m.run(safepoint_hook=_pause_first_entry(m, self.fid))
        return m

    def _snapshot(self, m):
        clo_versions = dict(m.heap.closures)
        rec_shapes = {oid: (tuple(slots), m.heap.rec_schema[oid])
                      for oid, slots in m.heap.records.items()}
        return {
            "current": m.table.snapshot(),
            "closures": clo_versions,
            "records": rec_shapes,
            "frame_versions": [f.version for f in m.stack],
            "steps": m.steps,
            "events": list(m.events),
            "has_v2": m.table.has(self.fid, "v2"),
        }

    def test_rollback_all_injection_points(self):
        for point in ("validate", "stage", "commit"):
            m = self._paused_machine()
            snap = self._snapshot(m)
            with self.assertRaises(errors.UpgradeAborted) if point != "validate" \
                    else self.assertRaises(errors.UpgradeAborted):
                upgrade_at_safepoint(
                    m, "v2", {self.fid: self.v2}, self.ops,
                    inject_failure=point,
                )
            # 版本表未发布
            self.assertEqual(m.table.snapshot(), snap["current"])
            self.assertFalse(m.table.has(self.fid, "v2"))
            # 闭包载荷与记录布局未变
            self.assertEqual(dict(m.heap.closures), snap["closures"])
            self.assertEqual(
                {oid: (tuple(slots), m.heap.rec_schema[oid])
                 for oid, slots in m.heap.records.items()},
                snap["records"],
            )
            self.assertEqual([f.version for f in m.stack], snap["frame_versions"])
            # 回滚后仍可继续跑出*原 v1* 结果：基线读共享 c=4，f(4)=14
            m.run()
            self.assertEqual(
                m.events,
                [("set", "c", 1), ("set", "c", 2), ("set", "c", 3), ("set", "c", 4)],
                point,
            )
            self.assertEqual(m.result, 14, point)

    def test_validation_failure_leaves_everything_untouched(self):
        m = self._paused_machine()
        snap = self._snapshot(m)
        bad = replace(self.v2, params=("z",))
        with self.assertRaises(errors.UpgradeValidationError):
            upgrade_at_safepoint(
                m, "v2", {self.fid: bad}, self.ops,
            )
        self.assertEqual(m.table.snapshot(), snap["current"])
        self.assertEqual(dict(m.heap.closures), snap["closures"])
        m.run()
        self.assertEqual(m.result, 14)


if __name__ == "__main__":
    unittest.main()

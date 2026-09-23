"""版本表、字段迁移规则与安全点热升级。

重点逐条覆盖：
* 互递归组成员集合/元数/内容哈希校验；
* 两个闭包共享一个计数器 cell，迁移后别名不复制；
* 旧版本栈帧持有共享 cell、迁移后新闭包再次修改该 cell、且在安全点中断
  （同一执行例中三者都出现）；
* 恢复提交前注入失败 → 完整回滚（机器与提交前逐位一致）。
"""

import json
import os
import sys
import unittest

from recenv import (
    Machine,
    MigrationRules,
    convert,
    errors,
    snapshot,
)
from recenv.heap import CellObj, ClosureObj, Ref

try:
    from tests_ext import progs
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from tests_ext import progs


def pause_when(machine, pred, cap=400):
    machine.start()
    for _ in range(cap):
        machine.run_segment(1)
        if pred(machine.call_trace()):
            return
    raise AssertionError("pause condition never reached")


def closures_of(machine, unit, version):
    out = []
    for i in machine.heap.all_oids():
        o = machine.heap.get(Ref(i))
        if isinstance(o, ClosureObj):
            e = machine.bundle.get(o.fkey)
            if e.unit == unit and e.version == version:
                out.append(o)
    return out


class TestVersionTable(unittest.TestCase):
    def test_content_hash_stable_and_distinct(self):
        from recenv.codebundle import build_bundle, expr_hash

        p1 = convert(progs.standalone_v1(3))
        b = build_bundle(p1)
        bump = next(e for e in b.entries() if e.unit == "bump")
        # 同内容 → 同哈希
        self.assertEqual(bump.code_hash, expr_hash(bump.body))
        # 不同版本代码 → 不同哈希
        p2 = convert(progs.standalone_v2(3))
        bump2_entry = next(e for e in build_bundle(p2).entries() if e.unit == "bump")
        self.assertNotEqual(bump.code_hash, bump2_entry.code_hash)

    def test_successor_version_must_advance_by_one(self):
        from recenv.codebundle import build_bundle

        b = build_bundle(convert(progs.standalone_v1(2)))
        b.import_program(convert(progs.standalone_v2(2)), "x:")  # 提供 @2
        b.import_program(convert(_bump3()), "v3:")               # 提供 @3
        v1 = next(e for e in b.entries() if e.unit == "bump" and e.version == 1)
        v2 = next(e for e in b.entries() if e.unit == "bump" and e.version == 2)
        v3 = next(e for e in b.entries() if e.unit == "bump" and e.version == 3)
        # @1 -> @3 非法（必须恰好 +1）
        with self.assertRaises(errors.CodeVersionError):
            b.check_successor(v1, v3)
        # @1 -> @2、@2 -> @3 合法
        b.check_successor(v1, v2)
        b.check_successor(v2, v3)

    def test_group_member_set_and_arity_fixed(self):
        # eo 组成员集合必须一致（ev/od），元数一致；由 check_successor 保证。
        from recenv.codebundle import build_bundle

        b = build_bundle(convert(progs.mutual_v1(3)))
        b.import_program(convert(progs.mutual_v2(3)), "n:")
        for m in ("ev", "od"):
            old = b.members("eo", 1)[m]
            new = b.members("eo", 2)[m]
            b.check_successor(old, new)  # 不抛即通过


def _bump3():
    from recenv import ast_nodes as A
    bump = A.Lambda(
        ["x"],
        A.Set("kount", A.Add(A.Add(A.Var("kount"), A.Var("x")), A.Var("bias"))),
        ver="bump@3",
    )
    drive = A.Lambda(
        ["n"],
        A.If0(
            A.Var("n"), A.Int(0),
            A.Add(A.Call(A.Var("bump"), [A.Int(1)]),
                  A.Call(A.Var("drive"), [A.Add(A.Var("n"), A.Int(-1))])),
        ),
    )
    return A.Let(
        "bias", A.Int(100),
        A.Let("kount", A.Int(0),
        A.Let("bump", bump,
        A.LetRec([A.RecBinding("drive", drive)],
                 A.Add(A.Call(A.Var("drive"), [A.Int(1)]), A.Var("kount"))))))


class TestMigrationRulesValidation(unittest.TestCase):
    def test_default_must_be_constant_int(self):
        with self.assertRaises(errors.MigrationError):
            MigrationRules(add={"b": True})           # bool 拒绝
        with self.assertRaises(errors.MigrationError):
            MigrationRules(add={"b": 1.5})            # 非整数
        with self.assertRaises(errors.MigrationError):
            MigrationRules(add={"b": "x"})

    def test_rule_conflicts_rejected(self):
        with self.assertRaises(errors.MigrationError):
            MigrationRules(rename={"c": "k"}, delete=("c",))
        with self.assertRaises(errors.MigrationError):
            MigrationRules(rename={"c": "b"}, add={"b": 1})
        with self.assertRaises(errors.MigrationError):
            MigrationRules(delete=("c",), add={"c": 1})

    def test_delete_still_referenced_rejected(self):
        p1 = convert(progs.mutual_v1(3))
        p2 = convert(progs.mutual_v2(3))
        m = Machine(p1)
        pause_when(m, lambda t: any(u == "eo" for _, u, _ in t))
        m.request_upgrade(
            "eo", p2,
            MigrationRules(rename={"c": "kount"}, add={"bias": 1000},
                           delete=("kount",)),
        )
        with self.assertRaises(errors.MigrationError):
            m.run_segment(1)
        self.assertEqual(m.status, "paused")  # 失败后留在安全点

    def test_unclosed_field_set_rejected(self):
        # 不给 add(bias)：新代码需要 bias 但规则无法闭合字段集
        p1 = convert(progs.standalone_v1(2))
        p2 = convert(progs.standalone_v2(2))
        m = Machine(p1)
        pause_when(m, lambda t: any(u == "bump" for _, u, _ in t))
        m.request_upgrade("bump", p2, MigrationRules(rename={"c": "kount"}))
        with self.assertRaises(errors.MigrationError):
            m.run_segment(1)
        self.assertEqual(m.status, "paused")

    def test_bad_version_gap_rejected_at_upgrade(self):
        p1 = convert(progs.standalone_v1(2))
        p3 = convert(_bump3())  # bump@3，缺 @2
        m = Machine(p1)
        pause_when(m, lambda t: any(u == "bump" for _, u, _ in t))
        m.request_upgrade("bump", p3, MigrationRules())
        with self.assertRaises(errors.RecenvError):
            m.run_segment(1)
        self.assertEqual(m.status, "paused")


class TestStandaloneMigration(unittest.TestCase):
    def test_shared_cell_and_mixed_versions_in_one_run(self):
        """强制场景（独立函数版）：旧栈帧持有共享 cell → 迁移 → 迁移后的新闭包
        再次修改该 cell（旁路调用）→ 旧栈继续跑完；同一执行例三者都出现。"""
        p1 = convert(progs.standalone_v1(3))
        p2 = convert(progs.standalone_v2(2, bias=100))
        m = Machine(p1)
        # 先停在 bump@1 帧在栈上的安全点（其内部 IIFE 调用入口）
        pause_when(m, lambda t: any(u == "bump" and v == 1 for _, u, v in t))

        old = closures_of(m, "bump", 1)
        self.assertEqual(len(old), 1)
        old_cell = m.heap.record(old[0].record).slots[0]
        self.assertIsInstance(m.heap.get(old_cell), CellObj)
        self.assertEqual(m.heap.cell(old_cell).value, 0)

        # 在当前安全点登记并立即应用升级（不推进执行）
        m.request_upgrade("bump", p2,
                          MigrationRules(rename={"c": "kount"}, add={"bias": 100}))
        handles = m.apply_pending_upgrade()
        # 旧 bump@1 栈帧仍然在栈上、钉在版本 1
        self.assertIn(("bump", 1), [(u, v) for _, u, v in m.call_trace()])
        self.assertEqual(m.applied_upgrades, [("bump", 1, 2)])

        # 迁移后的新闭包：kount 槽必须就是旧 cell（别名保持，不是副本）
        new = closures_of(m, "bump", 2)
        self.assertEqual(len(new), 1)
        entry = m.bundle.get(new[0].fkey)
        k_slot = m.heap.record(new[0].record).slots[
            list(entry.free_names).index("kount")
        ]
        self.assertEqual(k_slot, old_cell)
        self.assertEqual(
            m.heap.record(new[0].record).slots[
                list(entry.free_names).index("bias")],
            100,
        )

        # 同一执行例：旧栈仍暂停时旁路运行新闭包，它修改同一个共享 cell
        m.invoke(handles[0]["$self"], [1], side=True)
        side_val, side_events = m.run_side()
        self.assertEqual(m.status, "paused")
        self.assertEqual(side_val, 101)                  # bump@2(1): 0+1+100
        self.assertEqual(side_events, [("set", "kount", 101)])
        self.assertEqual(m.heap.cell(old_cell).value, 101)
        # 旧 bump@1 帧仍在
        self.assertIn(("bump", 1), [(u, v) for _, u, v in m.call_trace()])

        # 恢复旧执行：bump@1 对 c 的读在安全点之后（IIFE 内），因此它读到
        # 旁路写入的 101 并 +1 => 102；之后 drive 两次迭代经改线槽进入
        # bump@2：102+1+100=203，203+1+100=304。
        value, events = m.run_to_completion()
        self.assertEqual(
            events,
            [("set", "kount", 101), ("set", "c", 102),
             ("set", "kount", 203), ("set", "kount", 304)],
        )
        # drive 和 = 102 + 203 + 304 = 609；+最终 cell 304 => 913
        self.assertEqual(value, 913)
        self.assertEqual(m.heap.cell(old_cell).value, 304)

    def test_new_calls_after_upgrade_use_new_version(self):
        # 升级在第一次 bump 调用入口前已登记：升级在该安全点生效，本次及之后
        # 的 bump 调用都经新版本（调用方槽已改线）。
        p1 = convert(progs.standalone_v1(3))
        p2 = convert(progs.standalone_v2(3, bias=100))
        m = Machine(p1)
        m.request_upgrade("bump", p2,
                          MigrationRules(rename={"c": "kount"}, add={"bias": 100}))
        value, events = m.run_to_completion()
        # bump@2: 101, 202, 303；drive 和 = 606；+最终 cell 303 => 909
        self.assertEqual(value, 909)
        self.assertEqual(
            events,
            [("set", "kount", 101), ("set", "kount", 202), ("set", "kount", 303)],
        )


class TestMutualMigration(unittest.TestCase):
    def test_two_closures_share_counter_and_rec_wiring(self):
        p1 = convert(progs.mutual_v1(5))
        p2 = convert(progs.mutual_v2(3, bias=1000))
        m = Machine(p1)
        pause_when(m, lambda t: any(u == "eo" and v == 1 for _, u, v in t))

        ev1 = [c for c in closures_of(m, "eo", 1)
               if m.bundle.get(c.fkey).member == "ev"][0]
        c_slot = m.heap.record(ev1.record).slots[
            list(m.bundle.get(ev1.fkey).free_names).index("c")
        ]

        m.request_upgrade("eox", p2, MigrationRules())  # 错单元名
        with self.assertRaises(errors.CodeVersionError):
            m.run_segment(1)
        # 失败后重新登记正确升级
        m.request_upgrade("eo", p2,
                          MigrationRules(rename={"c": "kount"}, add={"bias": 1000}))
        m.run_segment(1)
        self.assertEqual(m.applied_upgrades[-1], ("eo", 1, 2))

        new = {m.bundle.get(c.fkey).member: c
               for c in closures_of(m, "eo", 2)}
        self.assertEqual(set(new), {"ev", "od"})
        ev2, od2 = new["ev"], new["od"]
        e2, o2 = m.bundle.get(ev2.fkey), m.bundle.get(od2.fkey)
        es, oss = m.heap.record(ev2.record).slots, m.heap.record(od2.record).slots

        # 共享计数器：ev2 的 kount 就是旧 c cell
        self.assertEqual(
            es[list(e2.free_names).index("kount")], c_slot
        )
        # rec-cell：ev2 的 od 槽填充 od2；od2 的 ev 槽填充 ev2（互指不混淆）
        self.assertEqual(
            m.heap.cell(es[list(e2.free_names).index("od")]).value, Ref(od2.oid)
        )
        self.assertEqual(
            m.heap.cell(oss[list(o2.free_names).index("ev")]).value, Ref(ev2.oid)
        )
        # 新增常量字段
        self.assertEqual(es[list(e2.free_names).index("bias")], 1000)

        # 旧组的 rec-cell 未被改线（在途旧帧继续旧版本）
        self.assertTrue(
            all(u == "eo" and v == 1 for _, u, v in m.call_trace()
                if u == "eo")
        )


class TestRollback(unittest.TestCase):
    def _state(self, m):
        d = snapshot.machine_to_snapshot(m)
        return json.dumps(
            {k: d[k] for k in (
                "heap", "code", "frames", "events", "steps", "depth",
                "control", "env")},
            sort_keys=True, ensure_ascii=True,
        )

    def test_every_failpoint_rolls_back_byte_identically(self):
        p1 = convert(progs.mutual_v1(5))
        p2 = convert(progs.mutual_v2(3))
        rules = MigrationRules(rename={"c": "kount"}, add={"bias": 1000})
        for fp in ("before_register", "after_shadow_build", "before_commit_code",
                   "before_commit_objects", "before_rewire", "after_rewire"):
            m = Machine(p1)
            pause_when(m, lambda t: any(u == "eo" and v == 1 for _, u, v in t))
            before = self._state(m)
            m.request_upgrade("eo", p2, rules, failpoint=fp)
            try:
                m.run_segment(1)
            except errors.RecenvError:
                pass
            self.assertEqual(m.status, "paused", fp)
            self.assertEqual(self._state(m), before, fp)
            self.assertIsNone(m._pending)
            # 仍可继续，且结果与完全不升级一致
            v, ev = m.run_to_completion()
            self.assertEqual(v, 9, fp)
            self.assertEqual(
                ev, [("set", "c", 1), ("set", "c", 2), ("set", "c", 3)], fp
            )


if __name__ == "__main__":
    unittest.main()

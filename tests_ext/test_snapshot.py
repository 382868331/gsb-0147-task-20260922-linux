"""快照序列化/恢复：跨进程、引用图校验、别名与环、损坏拒绝。"""

import copy
import json
import os
import sys
import unittest

from recenv import Machine, convert, errors, eval_target, snapshot
from recenv.heap import CellObj, ClosureObj, RecordObj, Ref

try:
    from proggen import Generator
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
    from proggen import Generator


def cross_process(program, n_pauses, budget, step_limit=10_000):
    m = Machine(program, step_limit=step_limit)
    for _ in range(n_pauses):
        if m.run_segment(budget) == "done":
            return m.result, m.events
        # 序列化为文本再恢复，模拟全新进程（无共享 Python 对象）
        m = snapshot.loads(snapshot.dumps(m))
    return m.run_to_completion()


class TestSnapshotResume(unittest.TestCase):
    def test_cross_process_plain(self):
        for seed in range(40):
            a = Generator(40000 + seed).gen_program(max_nodes=150)
            pr = convert(a)
            tv, te = eval_target(pr)
            for n_p in (1, 3, 7):
                for bud in (1, 3, 11):
                    mv, me = cross_process(pr, n_p, bud)
                    self.assertEqual((tv, te), (mv, me), (seed, n_p, bud))

    def test_cross_process_letrec(self):
        for seed in range(20):
            a = Generator(50000 + seed).gen_letrec_program(max_nodes=200, n_funs=2)
            pr = convert(a)
            tv, te = eval_target(pr)
            for n_p in (1, 3):
                for bud in (1, 3, 11):
                    mv, me = cross_process(pr, n_p, bud)
                    self.assertEqual((tv, te), (mv, me), (seed, n_p, bud))

    def test_multiple_pauses_same_result(self):
        a = Generator(909090).gen_letrec_program(max_nodes=220, n_funs=3)
        pr = convert(a)
        tv, te = eval_target(pr)
        self.assertEqual(cross_process(pr, 25, 2), (tv, te))

    def test_roundtrip_is_independent_of_json_key_order(self):
        # 手工构造含共享 cell 与互指 rec-cell 的暂停态，置换堆键顺序。
        from tests_ext.progs import mutual_v1

        pr = convert(mutual_v1(5))
        m = Machine(pr)
        self.assertEqual(m.run_segment(1), "paused")

        def alias_graph(mm):
            edges = set()
            for i in mm.heap.all_oids():
                o = mm.heap.get(Ref(i))
                if isinstance(o, ClosureObj):
                    for s in mm.heap.record(o.record).slots:
                        if isinstance(s, Ref):
                            edges.add((i, s.oid))
            return frozenset(edges)

        g = alias_graph(m)
        base = json.loads(snapshot.dumps(m))
        import random
        rng = random.Random(1234)
        expected = (9, [("set", "c", 1), ("set", "c", 2), ("set", "c", 3)])
        for _ in range(15):
            keys = list(base["heap"])
            rng.shuffle(keys)
            d = dict(base)
            d["heap"] = {k: base["heap"][k] for k in keys}
            m2 = snapshot.loads(json.dumps(d))
            self.assertEqual(alias_graph(m2), g)
            self.assertEqual(m2.run_to_completion(), expected)


class TestReferenceGraphValidation(unittest.TestCase):
    def _paused(self, program):
        m = Machine(program)
        m.run_segment(2)
        return json.loads(snapshot.dumps(m))

    def test_dangling_ref_rejected(self):
        a = Generator(40000).gen_program(max_nodes=150)
        d = self._paused(convert(a))
        # 把任一可达记录的某个槽改成指向不存在的 OID（保证悬空引用被根遍历到）
        changed = False
        for rec in d["heap"].values():
            if rec[0] != "record":
                continue
            for j, s in enumerate(rec[3]):
                if s[0] == "r":
                    rec[3][j] = ["r", 99_999_999]
                    changed = True
                    break
            if changed:
                break
        self.assertTrue(changed)
        with self.assertRaises(errors.SnapshotError):
            snapshot.loads(json.dumps(d))

    def test_tampered_code_hash_rejected(self):
        a = Generator(40000 + 1).gen_program(max_nodes=150)
        d = self._paused(convert(a))
        # 篡改某函数体的哈希声明（不改体）→ 重算哈希不符必须拒绝
        fkey = next(iter(d["code"]["functions"]))
        d["code"]["functions"][fkey]["hash"] = "0" * 64
        with self.assertRaises(errors.SnapshotError):
            snapshot.loads(json.dumps(d))

    def test_tampered_code_body_rejected(self):
        a = Generator(40000 + 2).gen_program(max_nodes=150)
        d = self._paused(convert(a))
        fkey = next(iter(d["code"]["functions"]))
        # 把函数体替换为常量 12345（哈希声明未改）→ 重算哈希不符
        d["code"]["functions"][fkey]["body"] = ["node", "TConst",
                                                [["value", ["int", 12345]]]]
        with self.assertRaises(errors.SnapshotError):
            snapshot.loads(json.dumps(d))

    def test_unknown_function_in_frame_rejected(self):
        a = Generator(40000 + 3).gen_program(max_nodes=150)
        d = self._paused(convert(a))
        # 改一个闭包的 fkey 为不存在的键
        for oid, rec in d["heap"].items():
            if rec[0] == "closure":
                rec[1] = "does-not-exist"
                break
        with self.assertRaises(errors.SnapshotError):
            snapshot.loads(json.dumps(d))

    def test_bad_format_rejected(self):
        with self.assertRaises(errors.SnapshotError):
            snapshot.loads(json.dumps({"format": "nope"}))
        with self.assertRaises(errors.SnapshotError):
            snapshot.loads("{not json")

    def test_alias_preserved_not_duplicated(self):
        from tests_ext.progs import mutual_v1

        pr = convert(mutual_v1(4))
        m = Machine(pr)
        m.run_segment(1)
        # ev 与 od 经组帧的 rec-cell 成环；恢复后仍然成环且对象身份一致
        m2 = snapshot.loads(snapshot.dumps(m))
        clos = [m2.heap.get(Ref(i)) for i in m2.heap.all_oids()
                if isinstance(m2.heap.get(Ref(i)), ClosureObj)
                and m2.bundle.get(m2.heap.get(Ref(i)).fkey).unit == "eo"]
        by_member = {m2.bundle.get(c.fkey).member: c for c in clos}
        ev, od = by_member["ev"], by_member["od"]
        ev_slots = m2.heap.record(ev.record).slots
        od_slots = m2.heap.record(od.record).slots
        ev_entry = m2.bundle.get(ev.fkey)
        od_entry = m2.bundle.get(od.fkey)
        od_cell = m2.heap.cell(ev_slots[list(ev_entry.free_names).index("od")])
        ev_cell = m2.heap.cell(od_slots[list(od_entry.free_names).index("ev")])
        self.assertEqual(od_cell.value, Ref(od.oid))
        self.assertEqual(ev_cell.value, Ref(ev.oid))
        # 外部共享 cell c：ev 的 c 槽
        c_ref = ev_slots[list(ev_entry.free_names).index("c")]
        self.assertIsInstance(m2.heap.get(c_ref), CellObj)


if __name__ == "__main__":
    unittest.main()

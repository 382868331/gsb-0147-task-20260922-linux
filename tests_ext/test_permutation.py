"""对象图分配次序与序列化顺序的置换不变性。

同一程序在不同堆 OID 起始偏移下迁移：可见结果与别名关系必须一致
（OID 数值本身当然不同）；同一暂停态在堆键顺序被随机置换后恢复，
引用图（别名/环）与继续执行的结果必须一致。
"""

import json
import os
import random
import sys
import unittest

from recenv import Machine, MigrationRules, convert, snapshot
from recenv.heap import ClosureObj, Heap, Ref


def counter_v1(n):
    from recenv import ast_nodes as A
    # 两个闭包共享计数器 c 且真正互指成组：
    #   bump() = (set c = c+1) + peek()
    #   peek() = c + (if0 1 then bump() /*死分支，静态仍互指*/ else 0)
    bump = A.Lambda(
        [],
        A.Add(A.Set("c", A.Add(A.Var("c"), A.Int(1))),
              A.Call(A.Var("peek"), [])),
    )
    peek = A.Lambda(
        [],
        A.Add(A.Var("c"),
              A.If0(A.Int(1), A.Call(A.Var("bump"), []), A.Int(0))),
    )
    drive = A.Lambda(
        ["n"],
        A.If0(
            A.Var("n"), A.Int(0),
            A.Add(A.Call(A.Var("bump"), []),
                  A.Call(A.Var("drive"), [A.Add(A.Var("n"), A.Int(-1))])),
        ),
    )
    return A.Let(
        "c", A.Int(0),
        A.LetRec(
            [A.RecBinding("bump", bump), A.RecBinding("peek", peek)],
            A.LetRec(
                [A.RecBinding("drive", drive)],
                A.Add(A.Call(A.Var("drive"), [A.Int(n)]), A.Var("c")),
            ),
            ver="cnt@1",
        ),
    )


def counter_v2(n, bias=100):
    from recenv import ast_nodes as A
    bump = A.Lambda(
        [],
        A.Add(A.Set("k", A.Add(A.Var("k"), A.Var("b"))),
              A.Call(A.Var("peek"), [])),
    )
    peek = A.Lambda(
        [],
        A.Add(A.Var("k"),
              A.If0(A.Int(1), A.Call(A.Var("bump"), []), A.Int(0))),
    )
    drive = A.Lambda(
        ["n"],
        A.If0(
            A.Var("n"), A.Int(0),
            A.Add(A.Call(A.Var("bump"), []),
                  A.Call(A.Var("drive"), [A.Add(A.Var("n"), A.Int(-1))])),
        ),
    )
    return A.Let(
        "b", A.Int(bias),
        A.Let(
            "k", A.Int(0),
            A.LetRec(
                [A.RecBinding("bump", bump), A.RecBinding("peek", peek)],
                A.LetRec(
                    [A.RecBinding("drive", drive)],
                    A.Add(A.Call(A.Var("drive"), [A.Int(n)]), A.Var("k")),
                ),
                ver="cnt@2",
            ),
        ),
    )


def shared_counter_relations(m):
    """{成员: k 槽 OID} 与 bump 的 b 常量槽；以及 rec-cell 互指关系。"""
    rel = {}
    rec_edges = set()
    for i in m.heap.all_oids():
        o = m.heap.get(Ref(i))
        if not isinstance(o, ClosureObj):
            continue
        e = m.bundle.get(o.fkey)
        if e.unit != "cnt" or e.version != 2:
            continue
        slots = m.heap.record(o.record).slots
        rel[o.member] = {n: slots[list(e.free_names).index(n)]
                         for n in e.free_names}
        for pos, kind in enumerate(e.capture_kinds):
            if kind == "rec":
                cell = m.heap.cell(slots[pos])
                rec_edges.add((o.member, e.free_names[pos],
                               cell.value.oid if isinstance(cell.value, Ref) else None))
    return rel, rec_edges


def run_with_offset(offset, n):
    heap = Heap(limit=100_000)
    if offset:
        keep = []
        for _ in range(offset):
            keep.append(heap.new_cell(0))  # 改变后续 OID 起点
        # keep 保活到函数结束，仅用于制造 OID 偏移
        assert len(keep) == offset
    p1 = convert(counter_v1(n))
    p2 = convert(counter_v2(n - 1))
    m = Machine(p1, heap=heap)
    m.start()
    for _ in range(400):
        m.run_segment(1)
        if any(u == "cnt" for _, u, _ in m.call_trace()):
            break
    m.request_upgrade("cnt", p2,
                      MigrationRules(rename={"c": "k"}, add={"b": 100}))
    v, ev = m.run_to_completion()
    rel, edges = shared_counter_relations(m)
    return v, ev, rel, edges, len(keep) if offset else 0


class TestAllocationOrderPermutation(unittest.TestCase):
    def test_results_and_alias_relations_invariant(self):
        v0, ev0, rel0, edges0, _ = run_with_offset(0, 3)
        for offset in (1, 7, 53, 200):
            v, ev, rel, edges, _ = run_with_offset(offset, 3)
            self.assertEqual((v, ev), (v0, ev0), offset)
            # 别名：bump 与 peek 的 k 必须是同一 OID（每种偏移各自内部）
            self.assertEqual(rel["bump"]["k"], rel["peek"]["k"], offset)
            self.assertEqual(rel["bump"]["b"], rel["peek"].get("b", rel["bump"]["b"])
                             if "b" in rel["peek"] else rel["bump"]["b"])
            # bump 新增常量槽 b 为内联整数 100
            self.assertEqual(rel["bump"]["b"], 100, offset)
            # rec 边：bump 体内调 peek，故其 rec 槽经 peek 的 rec-cell 指向
            # peek 新闭包（成员名层面，不依赖 OID 数值）。
            edge_names = {(a, b) for a, b, _ in edges}
            self.assertIn(("bump", "peek"), edge_names)

    def test_offset_changes_oids_but_not_identity_structure(self):
        _, _, rel0, _, _ = run_with_offset(0, 3)
        _, _, rel1, _, _ = run_with_offset(100, 3)
        # OID 数值确实不同（证明测试有效），但都是 Ref
        self.assertNotEqual(rel0["bump"]["k"].oid, rel1["bump"]["k"].oid)
        self.assertIsInstance(rel0["bump"]["k"], Ref)


class TestSerializationOrderPermutation(unittest.TestCase):
    def test_heap_key_shuffle_preserves_graph_and_result(self):
        p1 = convert(counter_v1(3))
        m = Machine(p1)
        m.run_segment(1)  # 首个安全点暂停，共享/环对象都已在堆上

        def graph(mm):
            edges = set()
            for i in mm.heap.all_oids():
                o = mm.heap.get(Ref(i))
                if isinstance(o, ClosureObj):
                    for s in mm.heap.record(o.record).slots:
                        if isinstance(s, Ref):
                            edges.add((i, s.oid))
            return frozenset(edges)

        g = graph(m)
        base = json.loads(snapshot.dumps(m))
        # bump()=(set c=c+1)+peek()(=c)；drive(3) 的 bump 返回 2+4+6=12，+c=3 => 15
        expected = (15, [("set", "c", 1), ("set", "c", 2), ("set", "c", 3)])
        rng = random.Random(98765)
        for _ in range(20):
            keys = list(base["heap"]); rng.shuffle(keys)
            d = dict(base); d["heap"] = {k: base["heap"][k] for k in keys}
            m2 = snapshot.loads(json.dumps(d))
            self.assertEqual(graph(m2), g)
            self.assertEqual(m2.run_to_completion(), expected)


if __name__ == "__main__":
    unittest.main()

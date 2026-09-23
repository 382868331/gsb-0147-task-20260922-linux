"""执行镜像测试：共享 / 环 / 别名保持、置换不变性、损坏拒绝、跨进程恢复。

重点：
* letrec 互递归组帧 cell ↔ 成员闭包构成环，两阶段装载后仍是同一组 OID；
* 两个闭包共享一个计数器 cell，镜像往返后共享关系不被复制成两个对象；
* 对象条目排列顺序（序列化顺序）任意置换，装载出的对象图与续执行结果不变；
* 不同暂停切分（等价于对象图分配次序的确定性行为）产出相同对象图；
* 悬空引用 / 篡改代码坐标 / 代码版本缺失 / 非法续延全部被完整校验拒绝；
* 真正启动新进程（``python -m recenv._worker``）恢复，多次暂停结果一致。
"""

from __future__ import annotations

import copy
import json
import os
import random
import shutil
import tempfile
import unittest

from recenv import ast_nodes as ast
from recenv import convert, errors
from recenv import image as imgmod
from recenv import persist
from recenv.machine import Machine
from recenv.migration import fieldize


def _even_odd(n):
    ev = ast.Lambda(["n"], ast.If0(ast.Var("n"), ast.Int(1),
        ast.Call(ast.Var("odd"), [ast.Add(ast.Var("n"), ast.Int(-1))])))
    od = ast.Lambda(["n"], ast.If0(ast.Var("n"), ast.Int(0),
        ast.Call(ast.Var("even"), [ast.Add(ast.Var("n"), ast.Int(-1))])))
    return ast.LetRec([ast.RecBinding("even", ev), ast.RecBinding("odd", od)],
                      ast.Call(ast.Var("even"), [ast.Int(n)]))


def _counter():
    return ast.Let(
        "c", ast.Int(0),
        ast.Let("inc", ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))),
        ast.Let("get", ast.Lambda([], ast.Var("c")),
            ast.Add(ast.Call(ast.Var("inc"), []),
              ast.Add(ast.Call(ast.Var("inc"), []), ast.Call(ast.Var("get"), []))))),
    )


def shuffle_obj(o, rng):
    if isinstance(o, dict):
        items = list(o.items())
        rng.shuffle(items)
        return {k: shuffle_obj(v, rng) for k, v in items}
    if isinstance(o, list):
        return [shuffle_obj(x, rng) for x in o]
    return o


def alias_signature(m: Machine) -> set:
    """每个闭包捕获记录中出现的 cell OID 集合（按闭包 OID 计）。"""
    sig = set()
    for clo_oid, (fid, rec_oid, ver) in m.heap.closures.items():
        cells = tuple(sorted(s for s in m.heap.records[rec_oid]
                             if s in m.heap.cells))
        sig.add((clo_oid, fid, ver, cells))
    return sig


class TestRoundTrip(unittest.TestCase):
    def test_finished_round_trip(self):
        p = fieldize(convert(_even_odd(11)))
        m = Machine(p); m.run()
        m2 = imgmod.load_machine(imgmod.dump_machine(m))
        self.assertEqual(m2.state, "finished")
        self.assertEqual(m2.result, 0)
        self.assertEqual(m2.events, m.events)

    def test_mid_stack_round_trip_resume(self):
        p = fieldize(convert(_even_odd(21)))
        full = Machine(p); full.run()
        # 找一个明确停在调用栈中间（非安全点）的预算
        m = None
        for budget in range(30, 120):
            cand = Machine(p); cand.run(step_budget=budget)
            if not cand.at_safepoint and len(cand.stack) > 3:
                m = cand
                break
        self.assertIsNotNone(m)
        m2 = imgmod.load_machine(imgmod.dump_machine(m))
        self.assertEqual(m2.steps, m.steps)
        self.assertEqual([tuple(f.code) for f in m2.stack],
                         [tuple(f.code) for f in m.stack])
        m2.run()
        self.assertEqual((m2.result, m2.events, m2.steps),
                         (full.result, full.events, full.steps))

    def test_mutual_recursion_cycle_preserved(self):
        """组帧两个 cell 与两个成员闭包构成环：装载后环与共享保持。"""
        p = fieldize(convert(_even_odd(4)))
        m = Machine(p); m.run(step_budget=60)
        m2 = imgmod.load_machine(imgmod.dump_machine(m))
        rec_cells = {
            oid: [s for s in slots if s in m2.heap.cells]
            for oid, slots in m2.heap.records.items()
        }
        group = [oid for oid, cs in rec_cells.items() if len(cs) == 2]
        self.assertEqual(len(group), 1)
        cells = rec_cells[group[0]]
        # 每个成员闭包的捕获记录里装的是另一个成员的 cell（环）
        for cell in cells:
            clo = m2.heap.cells[cell].value
            self.assertIn(clo, m2.heap.closures)
            cap = m2.heap.closures[clo][1]
            other = cells[1] if cells[0] == cell else cells[0]
            self.assertIn(other, m2.heap.records[cap])

    def test_shared_counter_not_duplicated(self):
        p = fieldize(convert(_counter()))
        m = Machine(p); m.run(step_budget=12)
        m2 = imgmod.load_machine(imgmod.dump_machine(m))
        cell_oids = {s for slots in m2.heap.records.values()
                     for s in slots if s in m2.heap.cells}
        self.assertEqual(len(cell_oids), 1)
        m2.run()
        full = Machine(p); full.run()
        self.assertEqual((m2.result, m2.events), (full.result, full.events))

    def test_alias_signature_equal_after_roundtrip(self):
        p = fieldize(convert(_even_odd(7)))
        m = Machine(p); m.run(step_budget=80)
        before = alias_signature(m)
        m2 = imgmod.load_machine(imgmod.dump_machine(m))
        self.assertEqual(alias_signature(m2), before)


class TestPermutationInvariance(unittest.TestCase):
    def test_key_order_permutation(self):
        p = fieldize(convert(_even_odd(17)))
        full = Machine(p); full.run()
        m = Machine(p); m.run(step_budget=70)
        obj = imgmod.dump_machine(m)
        baseline = imgmod.load_machine(obj)
        base_data = (baseline.result, baseline.events, baseline.steps)
        for seed in range(12):
            perm = shuffle_obj(copy.deepcopy(obj), random.Random(seed))
            # JSON 文本字节不同（键序不同）……
            self.assertNotEqual(json.dumps(obj, sort_keys=True),
                                json.dumps(perm, sort_keys=False))
            mp = imgmod.load_machine(perm)
            self.assertEqual((mp.steps), m.steps)
            mp.run()
            self.assertEqual((mp.result, mp.events), (full.result, full.events))

    def test_different_pause_schedules_same_graph(self):
        # 不同步预算切分不改变确定性对象图；跑到同一总步数时堆数据段相同
        p = fieldize(convert(_even_odd(25)))
        target = 130

        def graph_at(budgets):
            m = Machine(p)
            used = 0
            for b in budgets:
                m.run(step_budget=b)
            if m.steps < target:
                m.run(step_budget=target - m.steps)
            d = imgmod.dump_machine(m)
            return d["data"], d["control"]["frames"]

        g1, f1 = graph_at([130])
        g2, f2 = graph_at([1, 2, 3, 5, 7, 11, 101])
        g3, f3 = graph_at([17, 29, 84])
        self.assertEqual(g1, g2)
        self.assertEqual(g1, g3)
        self.assertEqual(f1, f2)
        self.assertEqual(f1, f3)


class TestCorruptionRejected(unittest.TestCase):
    def _obj(self, budget=40):
        p = fieldize(convert(_even_odd(9)))
        m = Machine(p); m.run(step_budget=budget)
        return imgmod.dump_machine(m)

    def test_dangling_reference_rejected(self):
        obj = self._obj()
        # 把某个 record 槽指向不存在的 OID
        first_rec = next(iter(obj["data"]["records"]))
        obj["data"]["records"][first_rec]["slots"][0] = "o999999"
        with self.assertRaises(errors.ImageFormatError):
            imgmod.load_machine(obj)

    def test_missing_code_version_rejected(self):
        obj = self._obj()
        # 删掉某激活帧引用的版本：程序初始函数表与版本表都要去掉，
        # 否则自举表会重新引入它（加载器应报版本缺失，而非静默补全）
        fid = obj["control"]["frames"][-1]["fid"]
        ver = obj["control"]["frames"][-1]["version"]
        del obj["code"]["table"]["versions"][fid][ver]
        del obj["code"]["program"]["functions"][fid]
        with self.assertRaises(errors.ImageFormatError):
            imgmod.load_machine(obj)

    def test_tampered_code_coordinate_rejected(self):
        obj = self._obj()
        # 把 term_tag 改成与坐标实际节点不符的标签
        obj["control"]["frames"][-1]["term_tag"] = "TCall"
        with self.assertRaises(errors.ImageFormatError):
            imgmod.load_machine(obj)

    def test_path_out_of_range_rejected(self):
        obj = self._obj()
        obj["control"]["frames"][-1]["path"] = ["body", 99]
        with self.assertRaises(errors.ImageFormatError):
            imgmod.load_machine(obj)

    def test_bad_continuation_index_rejected(self):
        obj = self._obj()
        # 找到一个 call_arg 续延并篡改其 i
        def tamper(frames):
            for fr in frames:
                k = fr["k"]
                if k.get("k") == "call_arg":
                    k["i"] = 999
                    return True
                if isinstance(k.get("inner"), dict):
                    sub = [k["inner"]]
                    while sub:
                        kk = sub.pop()
                        if kk.get("k") == "call_arg":
                            kk["i"] = 999
                            return True
                        if isinstance(kk.get("inner"), dict):
                            sub.append(kk["inner"])
            return False
        if tamper(obj["control"]["frames"]):
            with self.assertRaises(errors.ImageFormatError):
                imgmod.load_machine(obj)

    def test_unknown_format_rejected(self):
        obj = self._obj()
        obj["format"] = "recenv-image/9"
        with self.assertRaises(errors.ImageFormatError):
            imgmod.load_machine(obj)

    def test_duplicate_oid_rejected(self):
        obj = self._obj()
        a = next(iter(obj["data"]["records"]))
        b = next(iter(obj["data"]["cells"]))
        obj["data"]["records"][b] = obj["data"]["records"][a]
        with self.assertRaises(errors.ImageFormatError):
            imgmod.load_machine(obj)

    def test_closure_record_type_mismatch_rejected(self):
        obj = self._obj()
        clo = next(iter(obj["data"]["closures"]))
        cell_oid = next(iter(obj["data"]["cells"]))
        obj["data"]["closures"][clo]["record"] = cell_oid
        with self.assertRaises(errors.ImageFormatError):
            imgmod.load_machine(obj)

    def test_self_certifying_flags_ignored(self):
        """镜像不能通过塞入任何布尔自述绕过校验（这里不存在这种字段，
        但即便篡改 filled/unfilled 不一致也必须被拒绝）。"""
        obj = self._obj()
        cell = next(iter(obj["data"]["cells"]))
        obj["data"]["cells"][cell]["filled"] = True
        obj["data"]["cells"][cell]["unfilled"] = True
        with self.assertRaises(errors.ImageFormatError):
            imgmod.load_machine(obj)


class TestCrossProcessResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recenv-img-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_multi_segment_new_processes(self):
        p_src = _even_odd(400)
        ck = os.path.join(self.tmp, "ck.json")
        persist.save_initial_image(p_src, ck, step_limit=1_000_000)
        statuses = []
        steps_chain = []
        for _ in range(400):
            r = persist.run_segment(ck, 200)
            statuses.append(r["status"])
            steps_chain.append(r["steps"])
            if r["status"] == "finished":
                break
        self.assertEqual(statuses[-1], "finished")
        self.assertEqual(r["result"], 1)  # even(400) = 1
        # 每段步数单调不减且确实经过多个新进程
        self.assertEqual(steps_chain, sorted(steps_chain))
        self.assertGreater(len(statuses), 5)

        # 同程序在进程内连续执行的结果一致
        m = Machine(fieldize(convert(p_src)), step_limit=1_000_000)
        m.run()
        self.assertEqual(m.result, r["result"])

    def test_cross_process_error_position(self):
        # 深递归后调用非函数：worker 必须带回 fid_trace 与节点类型
        fbody = ast.If0(
            ast.Var("n"), ast.Call(ast.Int(1), []),
            ast.Call(ast.Var("f"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        )
        root = ast.LetRec(
            [ast.RecBinding("f", ast.Lambda(["n"], fbody))],
            ast.Call(ast.Var("f"), [ast.Int(20)]),
        )
        ck = os.path.join(self.tmp, "err.json")
        persist.save_initial_image(root, ck, step_limit=1_000_000)
        r = persist.run_segment(ck, 1_000_000)
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["error"]["type"], "LangRuntimeError")
        self.assertEqual(r["error"]["ir_node"], "TCall")
        self.assertEqual(len(r["error"]["fid_trace"]), 21)
        # 出错后检查点文件仍是出错前镜像（未被错误结果覆盖）
        m = imgmod.load_image(ck)
        self.assertIn(m.state, ("paused", "finished"))


if __name__ == "__main__":
    unittest.main()

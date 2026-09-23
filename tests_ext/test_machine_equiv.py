"""显式栈机器：与目标解释器的等价性，以及分段暂停/恢复的确定性。

期望值来源：tests/proggen.py 生成程序，期望值由独立源/目标解释器给出，
机器结果只用于对照（参考实现不导入 machine）。
"""

import os
import random
import sys
import unittest

from recenv import Machine, convert, errors, eval_source, eval_target

try:
    from proggen import Generator
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
    from proggen import Generator


def segmented_result(program, budget_pattern, step_limit=10_000):
    m = Machine(program, step_limit=step_limit)
    i = 0
    while True:
        r = m.run_segment(budget_pattern[i % len(budget_pattern)])
        i += 1
        if r == "done":
            return m.result, list(m.events), m.steps


class TestMachineEquivalence(unittest.TestCase):
    def test_plain_generated_programs(self):
        n = 0
        for seed in range(120):
            a = Generator(1000 + seed).gen_program(max_nodes=180)
            pr = convert(a)
            try:
                tv, te = eval_target(pr)
            except errors.RecenvError:
                continue
            mv, me = Machine(pr).run_to_completion()
            self.assertEqual((tv, te), (mv, me), seed)
            n += 1
        self.assertGreater(n, 80)

    def test_letrec_generated_programs(self):
        for seed in range(60):
            a = Generator(5000 + seed).gen_letrec_program(max_nodes=220, n_funs=2)
            pr = convert(a)
            tv, te = eval_target(pr)
            mv, me = Machine(pr).run_to_completion()
            self.assertEqual((tv, te), (mv, me), seed)

    def test_triple_mutual(self):
        a = Generator(7777).gen_letrec_program(max_nodes=240, n_funs=3)
        pr = convert(a)
        self.assertEqual(eval_target(pr), Machine(pr).run_to_completion())


class TestSegmentedDeterminism(unittest.TestCase):
    def test_many_budget_splits(self):
        rng = random.Random(20260924)
        programs = []
        for seed in range(40):
            programs.append(Generator(20000 + seed).gen_program(max_nodes=150))
        for seed in range(20):
            programs.append(
                Generator(30000 + seed).gen_letrec_program(max_nodes=200, n_funs=2)
            )
        for a in programs:
            pr = convert(a)
            tv, te = eval_target(pr)
            for _ in range(6):
                pat = [rng.randint(1, 13) for _ in range(rng.randint(1, 5))]
                mv, me, ms = segmented_result(pr, pat)
                self.assertEqual(mv, tv, (pat, a))
                self.assertEqual(me, te, pat)

    def test_budget_1_is_call_entry_only(self):
        # 预算 1：每段至多跨过一个调用入口安全点；结果不变。
        a = Generator(31337).gen_letrec_program(max_nodes=220, n_funs=2)
        pr = convert(a)
        tv, te = eval_target(pr)
        mv, me, _ = segmented_result(pr, [1])
        self.assertEqual((tv, te), (mv, me))

    def test_exception_location_consistent_across_splits(self):
        # 调用整数必然抛 LangRuntimeError；异常前 bump 已 set 一次。
        # 对任何预算切分，异常类型与已发生事件序列都必须一致。
        from recenv import ast_nodes as A

        e = A.Let(
            "x", A.Int(0),
            A.Let(
                "bump",
                A.Lambda([], A.Set("x", A.Add(A.Var("x"), A.Int(1)))),
                A.Add(
                    A.Call(A.Var("bump"), []),
                    A.Call(A.Int(7), []),
                ),
            ),
        )
        pr = convert(e)
        for pat in ([1], [2], [3], [1, 5], [7, 2, 3]):
            m = Machine(pr)
            i = 0
            with self.assertRaises(errors.LangRuntimeError):
                while True:
                    m.run_segment(pat[i % len(pat)])
                    i += 1
                    if m.status == "done":
                        break
            self.assertEqual(m.events, [("set", "x", 1)], pat)


class TestStepAndFrameLimits(unittest.TestCase):
    def test_step_limit_preserves_events(self):
        from recenv import ast_nodes as A

        e = A.LetRec(
            [A.RecBinding("f", A.Lambda(
                ["n"],
                A.If0(
                    A.Var("n"),
                    A.Call(A.Var("f"), [A.Var("n")]),
                    A.Add(
                        A.Set("n", A.Int(0)),
                        A.Call(A.Var("f"), [A.Var("n")]),
                    ),
                )))],
            A.Call(A.Var("f"), [A.Int(1)]),
        )
        pr = convert(e)
        m = Machine(pr, step_limit=100)
        with self.assertRaises(errors.StepLimitError) as cm:
            m.run_to_completion()
        self.assertEqual(cm.exception.events, [("set", "n", 0)])
        self.assertEqual(cm.exception.limit, 100)

    def test_frame_limit_explicit(self):
        from recenv import ast_nodes as A

        body = A.If0(
            A.Var("n"), A.Int(0),
            A.Call(A.Var("f"), [A.Add(A.Var("n"), A.Int(-1))]),
        )
        e = A.LetRec(
            [A.RecBinding("f", A.Lambda(["n"], body))],
            A.Call(A.Var("f"), [A.Int(50)]),
        )
        pr = convert(e)
        m = Machine(pr, step_limit=10_000, frame_limit=10)
        with self.assertRaises(errors.FrameLimitError) as cm:
            m.run_to_completion()
        self.assertEqual(cm.exception.limit, 10)


if __name__ == "__main__":
    unittest.main()

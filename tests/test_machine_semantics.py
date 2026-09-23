"""显式栈执行机语义测试。

覆盖：
* 与独立源解释器对拍（返回值、副作用、精确步数）；
* 连续执行 vs 任意步预算分段暂停/恢复，可见结果、异常位置（fid_trace +
  IR 节点）、异常前事件、堆单元身份关系全部一致；
* 显式调用帧深达 500+ 不依赖 Python 递归栈。

期望值来自独立源解释器 source_interp（不导入 convert）。
"""

from __future__ import annotations

import random
import unittest

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source
from recenv.machine import Machine
from recenv.migration import fieldize

try:
    from proggen import Generator
except ImportError:  # pragma: no cover
    from .proggen import Generator


def _counter():
    return ast.Let(
        "c", ast.Int(0),
        ast.Let("inc", ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))),
        ast.Let("get", ast.Lambda([], ast.Var("c")),
            ast.Add(ast.Call(ast.Var("inc"), []),
              ast.Add(ast.Call(ast.Var("inc"), []), ast.Call(ast.Var("get"), []))))),
    )


def _even_odd(n):
    ev = ast.Lambda(["n"], ast.If0(ast.Var("n"), ast.Int(1),
        ast.Call(ast.Var("odd"), [ast.Add(ast.Var("n"), ast.Int(-1))])))
    od = ast.Lambda(["n"], ast.If0(ast.Var("n"), ast.Int(0),
        ast.Call(ast.Var("even"), [ast.Add(ast.Var("n"), ast.Int(-1))])))
    return ast.LetRec([ast.RecBinding("even", ev), ast.RecBinding("odd", od)],
                      ast.Call(ast.Var("even"), [ast.Int(n)]))


def _run_segmented(program, budgets, step_limit=10_000):
    m = Machine(program, step_limit=step_limit)
    for b in budgets:
        m.run(step_budget=b)
    m.run()
    return m


class TestMachineParity(unittest.TestCase):
    def test_hand_cases(self):
        for root, expect in [
            (_counter(), (5, [("set", "c", 1), ("set", "c", 2)])),
            (_even_odd(9), (0, [])),
            (_even_odd(8), (1, [])),
        ]:
            sv, se = eval_source(root)
            p = fieldize(convert(root))
            m = Machine(p)
            m.run()
            self.assertEqual((sv, se), expect)
            self.assertEqual((m.result, m.events), expect)

    def test_generated_plain_programs(self):
        n_ok = 0
        for seed in range(100):
            root = Generator(2000 + seed).gen_program(max_nodes=180)
            try:
                sv, se = eval_source(root, step_limit=10_000)
            except errors.RecenvError:
                continue
            p = fieldize(convert(root))
            m = Machine(p)
            m.run()
            self.assertEqual(m.result, sv, f"seed {seed}")
            self.assertEqual(m.events, se, f"seed {seed}")
            n_ok += 1
        self.assertGreater(n_ok, 70)

    def test_generated_letrec_programs(self):
        for seed in range(50):
            root = Generator(6000 + seed).gen_letrec_program(max_nodes=220, n_funs=2)
            sv, se = eval_source(root, step_limit=10_000)
            p = fieldize(convert(root))
            m = Machine(p)
            m.run()
            self.assertEqual((m.result, m.events), (sv, se), f"seed {seed}")

    def test_triple_mutual(self):
        root = Generator(8888).gen_letrec_program(max_nodes=240, n_funs=3)
        sv, se = eval_source(root)
        m = Machine(fieldize(convert(root)))
        m.run()
        self.assertEqual((m.result, m.events), (sv, se))


class TestPauseResumeDeterminism(unittest.TestCase):
    def test_counter_segmented_budgets(self):
        p = fieldize(convert(_counter()))
        full = Machine(p); full.run()
        # 多组预算切分，结果必须完全一致
        for seed in range(20):
            rng = random.Random(seed)
            m = Machine(p)
            while m.state == "paused":
                m.run(step_budget=rng.randint(1, 5))
            self.assertEqual(m.result, full.result, seed)
            self.assertEqual(m.events, full.events, seed)
            self.assertEqual(m.steps, full.steps, seed)

    def test_even_odd_mid_stack_pause(self):
        p = fieldize(convert(_even_odd(15)))
        m = Machine(p)
        m.run(step_budget=40)  # 停在调用栈中间，非安全点
        self.assertGreater(len(m.stack), 3)
        self.assertFalse(m.at_safepoint)
        trace_snapshot = [tuple(f.code) for f in m.stack]
        m2 = Machine(p)
        # 同预算连续跑的机器应与暂停机器逐帧同构
        m2.run(step_budget=40)
        self.assertEqual(
            [tuple(f.code) for f in m2.stack], trace_snapshot
        )
        m.run()
        m2.run()
        self.assertEqual((m.result, m.events, m.steps), (m2.result, m2.events, m2.steps))

    def test_exception_position_parity(self):
        # 运行期错误：调用非函数（深层递归到基线处触发）
        def root():
            fbody = ast.If0(
                ast.Var("n"),
                ast.Call(ast.Int(7), []),  # 基线处调用整数
                ast.Call(ast.Var("f"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            )
            return ast.LetRec(
                [ast.RecBinding("f", ast.Lambda(["n"], fbody))],
                ast.Call(ast.Var("f"), [ast.Int(4)]),
            )
        p = fieldize(convert(root()))
        fid = next(
            f for f in p.functions
            if any(x.startswith("cell:f#") for x in p.layout[f])
        )

        cont = Machine(p)
        with self.assertRaises(errors.LangRuntimeError) as cm:
            cont.run()
        e_cont = cm.exception

        rng = random.Random(3)
        seg = Machine(p)
        with self.assertRaises(errors.LangRuntimeError) as cm2:
            while seg.state == "paused":
                seg.run(step_budget=rng.randint(1, 9))
        e_seg = cm2.exception
        self.assertEqual(type(e_cont), type(e_seg))
        self.assertEqual(e_cont.fid_trace, e_seg.fid_trace)
        self.assertEqual(e_cont.ir_node, e_seg.ir_node)
        self.assertEqual(cont.events, seg.events)
        self.assertEqual(cont.steps, seg.steps)
        # f(4)->f(3)->f(2)->f(1)->f(0)，基线处报错时栈上 5 个同版本帧
        self.assertEqual(e_cont.fid_trace, [(fid, "v1")] * 5)
        self.assertEqual(e_cont.ir_node, "TCall")

    def test_step_limit_parity(self):
        loop = ast.LetRec(
            [ast.RecBinding("f",
             ast.Lambda(["n"], ast.Call(ast.Var("f"), [ast.Var("n")])))],
            ast.Call(ast.Var("f"), [ast.Int(0)]),
        )
        p = fieldize(convert(loop))
        m = Machine(p, step_limit=300)
        with self.assertRaises(errors.StepLimitError) as cm:
            m.run()
        self.assertEqual(cm.exception.limit, 300)
        self.assertEqual(cm.exception.steps, 301)

        # 分段累计到预算：最后一段必须在同一拍报同样的错
        m2 = Machine(p, step_limit=300)
        m2.run(step_budget=100)
        m2.run(step_budget=100)
        with self.assertRaises(errors.StepLimitError) as cm2:
            m2.run(step_budget=100)
        self.assertEqual(cm2.exception.steps, cm.exception.steps)


class TestDeepStacks(unittest.TestCase):
    def test_600_explicit_frames_no_python_recursion(self):
        # 500 是契约下限；这里取 600 留余量。显式栈，不碰 Python 递归深度。
        n = 600
        body = ast.If0(
            ast.Var("n"), ast.Int(1),
            ast.Add(ast.Int(1),
                    ast.Call(ast.Var("d"), [ast.Add(ast.Var("n"), ast.Int(-1))])),
        )
        root = ast.LetRec(
            [ast.RecBinding("d", ast.Lambda(["n"], body))],
            ast.Call(ast.Var("d"), [ast.Int(n)]),
        )
        sv, _ = eval_source(root, step_limit=100_000)
        m = Machine(fieldize(convert(root)), step_limit=100_000)
        m.run()
        self.assertEqual(m.result, sv)
        self.assertEqual(m.result, n + 1)

        # 暂停在最深点：分段跑到结束，记录显式激活栈帧峰值 >= n
        m2 = Machine(fieldize(convert(root)), step_limit=100_000)
        deepest = 0
        while m2.state == "paused":
            m2.run(step_budget=37)
            deepest = max(deepest, sum(1 for f in m2.stack if f.kind == "act"))
        self.assertGreaterEqual(deepest, n)


if __name__ == "__main__":
    unittest.main()

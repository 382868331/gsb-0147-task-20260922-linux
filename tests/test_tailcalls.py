"""尾部调用复用：深层尾递归不再累积调用层，非尾调用行为不变。

证据口径：两个解释器都记录**实际同时保留的最大函数调用层数**
（max_call_depth）。目标解释器对尾位置调用复用当前调用层，
深层尾递归时该值保持常数；源解释器（对照，行为不变）随深度线性
增长。两侧返回值与副作用事件序列必须逐项一致。
"""

import unittest

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source_with_stats, eval_target_with_stats

BIG_BUDGET = 200_000  # 步数预算：让深层用例不受 10000 步限制干扰


def countdown(n: int) -> ast.Expr:
    # letrec loop(n, acc) = if0 n then acc else loop(n + -1, acc + 1)
    # in loop(n, 0)   —— 完全尾递归
    loop = ast.Lambda(
        ["n", "acc"],
        ast.If0(
            ast.Var("n"),
            ast.Var("acc"),
            ast.Call(
                ast.Var("loop"),
                [
                    ast.Add(ast.Var("n"), ast.Int(-1)),
                    ast.Add(ast.Var("acc"), ast.Int(1)),
                ],
            ),
        ),
    )
    return ast.LetRec(
        [ast.RecBinding("loop", loop)],
        ast.Call(ast.Var("loop"), [ast.Int(n), ast.Int(0)]),
    )


def even_odd(n: int) -> ast.Expr:
    ev = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"),
            ast.Int(1),
            ast.Call(ast.Var("odd"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    od = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"),
            ast.Int(0),
            ast.Call(ast.Var("even"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    return ast.LetRec(
        [ast.RecBinding("even", ev), ast.RecBinding("odd", od)],
        ast.Call(ast.Var("even"), [ast.Int(n)]),
    )


def non_tail_sum(n: int) -> ast.Expr:
    # letrec s(n) = if0 n then 0 else s(n + -1) + n  —— 调用不在尾位置
    s = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"),
            ast.Int(0),
            ast.Add(
                ast.Call(ast.Var("s"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
                ast.Var("n"),
            ),
        ),
    )
    return ast.LetRec(
        [ast.RecBinding("s", s)],
        ast.Call(ast.Var("s"), [ast.Int(n)]),
    )


def tail_with_shared_cell(n: int) -> ast.Expr:
    # let c = 0 in
    # letrec bump(n, tick) = if0 n then c else bump(n + -1, set c = c + 1)
    # in bump(n, 0)
    # 尾调用实参中的 set 每轮写共享 cell；c 被闭包捕获，不能随现场复用释放。
    bump = ast.Lambda(
        ["n", "tick"],
        ast.If0(
            ast.Var("n"),
            ast.Var("c"),
            ast.Call(
                ast.Var("bump"),
                [
                    ast.Add(ast.Var("n"), ast.Int(-1)),
                    ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1))),
                ],
            ),
        ),
    )
    return ast.Let(
        "c",
        ast.Int(0),
        ast.LetRec(
            [ast.RecBinding("bump", bump)],
            ast.Call(ast.Var("bump"), [ast.Int(n), ast.Int(0)]),
        ),
    )


class TestTailCallReuse(unittest.TestCase):
    def test_deep_tail_recursion_constant_layers(self):
        n = 8000
        root = countdown(n)
        s_val, s_ev, s_st = eval_source_with_stats(root, step_limit=BIG_BUDGET)
        t_val, t_ev, t_st = eval_target_with_stats(
            convert(root), step_limit=BIG_BUDGET
        )
        self.assertEqual((s_val, s_ev), (t_val, t_ev))
        self.assertEqual(t_val, n)
        # 目标解释器：尾调用复用同一层，保留层数是有界常数
        self.assertLessEqual(t_st.max_call_depth, 2)
        # 对照：源解释器不做复用，保留层数随递归深度增长
        self.assertGreaterEqual(s_st.max_call_depth, n)
        # 减少的是运行占用而非调高上限：目标侧深度远小于对照侧
        self.assertLess(t_st.max_call_depth, s_st.max_call_depth // 100)

    def test_mutual_tail_recursion_constant_layers(self):
        n = 4001
        root = even_odd(n)
        s_val, s_ev, s_st = eval_source_with_stats(root, step_limit=BIG_BUDGET)
        t_val, t_ev, t_st = eval_target_with_stats(
            convert(root), step_limit=BIG_BUDGET
        )
        self.assertEqual((s_val, s_ev), (t_val, t_ev))
        self.assertEqual(t_val, 0)  # 奇数
        self.assertLessEqual(t_st.max_call_depth, 2)
        self.assertGreaterEqual(s_st.max_call_depth, n)

    def test_non_tail_call_layers_unchanged(self):
        # 非尾调用必须保持原行为：保留层数随深度增长，结果一致
        n = 1500
        root = non_tail_sum(n)
        s_val, s_ev, s_st = eval_source_with_stats(root, step_limit=BIG_BUDGET)
        t_val, t_ev, t_st = eval_target_with_stats(
            convert(root), step_limit=BIG_BUDGET
        )
        self.assertEqual((s_val, s_ev), (t_val, t_ev))
        self.assertEqual(t_val, n * (n + 1) // 2)
        self.assertGreaterEqual(t_st.max_call_depth, n)
        self.assertEqual(s_st.max_call_depth, t_st.max_call_depth)

    def test_captured_cell_survives_frame_reuse(self):
        n = 3000
        root = tail_with_shared_cell(n)
        s_val, s_ev, s_st = eval_source_with_stats(root, step_limit=BIG_BUDGET)
        t_val, t_ev, t_st = eval_target_with_stats(
            convert(root), step_limit=BIG_BUDGET
        )
        # 被捕获的共享 cell 仍在使用：每轮 set 都生效且顺序一致
        self.assertEqual((s_val, s_ev), (t_val, t_ev))
        self.assertEqual(t_val, n)
        self.assertEqual(len(t_ev), n)
        self.assertEqual(t_ev[-1], ("set", "c", n))
        self.assertLessEqual(t_st.max_call_depth, 2)

    def test_infinite_tail_recursion_hits_step_limit(self):
        # letrec f(x) = f(x) in f(0)：纯尾调用死循环必须触发步数预算，
        # 而不是 Python 递归深度错误（证明复用真实发生）
        f = ast.Lambda(["x"], ast.Call(ast.Var("f"), [ast.Var("x")]))
        root = ast.LetRec(
            [ast.RecBinding("f", f)], ast.Call(ast.Var("f"), [ast.Int(0)])
        )
        with self.assertRaises(errors.StepLimitError):
            eval_target_with_stats(convert(root), step_limit=1000)
        with self.assertRaises(errors.StepLimitError):
            eval_source_with_stats(root, step_limit=1000)


if __name__ == "__main__":
    unittest.main()


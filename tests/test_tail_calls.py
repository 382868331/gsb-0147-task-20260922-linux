"""尾调用消除：复用不再需要的调用现场。

这些用例直接检查目标解释器的 FrameStats：
* 深尾递归的峰值存活激活帧数恒为 1（减少的是运行占用）；
* 非尾（普通）调用仍然一层套一层，行为与层数都不变；
* 被闭包捕获的数据（val / 共享 Cell）在现场复用后依然存活；
* 返回值与副作用顺序与独立源解释器一致；尾位置上的错误语义不变。
"""

import unittest

from recenv import ast_nodes as ast
from recenv import ast_nodes as A
from recenv import convert, errors, eval_source, eval_target_tracked


def tail_loop_program(n):
    # letrec loop(n) = if0 n then 0 else loop(n - 1)
    body = ast.If0(
        ast.Var("n"),
        ast.Int(0),
        ast.Call(ast.Var("loop"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
    )
    return ast.LetRec(
        [ast.RecBinding("loop", ast.Lambda(["n"], body))],
        ast.Call(ast.Var("loop"), [ast.Int(n)]),
    )


def nontail_loop_program(n):
    # 递归调用被 Add 包裹：普通调用，不能复用现场
    body = ast.If0(
        ast.Var("n"),
        ast.Int(0),
        ast.Add(
            ast.Call(ast.Var("f"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ast.Int(0),
        ),
    )
    return ast.LetRec(
        [ast.RecBinding("f", ast.Lambda(["n"], body))],
        ast.Call(ast.Var("f"), [ast.Int(n)]),
    )


def iter_tcalls(node):
    """枚举 IR 中全部 TCall（IR 节点种类有限，按字段递归）。"""
    children = []
    for attr in ("func", "cond", "then_e", "else_e", "value", "body"):
        sub = getattr(node, attr, None)
        if hasattr(sub, "__dataclass_fields__"):
            children.append(sub)
    for attr in ("args", "inits", "env", "exprs"):
        for sub in getattr(node, attr, ()) or ():
            if hasattr(sub, "__dataclass_fields__"):
                children.append(sub)
    if isinstance(node, A.TCall):
        yield node
    for sub in children:
        yield from iter_tcalls(sub)


class TestTailCallElimination(unittest.TestCase):
    def test_deep_self_tail_recursion_keeps_one_frame(self):
        # 50000 次尾递归：若每层都占现场，需要 5 万层 Python 栈；
        # 尾调用消除后峰值存活激活帧恒为 1，且结果正确。
        n = 50_000
        program = convert(tail_loop_program(n))
        value, events, stats = eval_target_tracked(program, step_limit=10_000_000)
        self.assertEqual(value, 0)
        self.assertEqual(events, [])
        self.assertEqual(stats.peak_depth, 1)
        self.assertEqual(stats.depth, 0)  # 运行结束后所有帧都已释放
        self.assertEqual(stats.tail_reuses, n)
        self.assertEqual(stats.calls, n + 1)  # 主层首次调用 + n 次复用

    def test_tail_recursion_matches_source(self):
        n = 200
        root = tail_loop_program(n)
        s_value, s_events = eval_source(root, step_limit=10_000)
        t_value, t_events, stats = eval_target_tracked(
            convert(root), step_limit=10_000
        )
        self.assertEqual((s_value, s_events), (t_value, t_events))
        self.assertEqual(stats.peak_depth, 1)

    def test_mutual_tail_recursion(self):
        # even/odd 互递归全部在尾位置：跨函数的尾调用也只占一层
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
        n = 20_001
        root = ast.LetRec(
            [ast.RecBinding("even", ev), ast.RecBinding("odd", od)],
            ast.Call(ast.Var("even"), [ast.Int(n)]),
        )
        value, events, stats = eval_target_tracked(convert(root), step_limit=10_000_000)
        self.assertEqual(value, 0)  # 奇数
        self.assertEqual(stats.peak_depth, 1)
        self.assertEqual(stats.tail_reuses, n)
        s_root = ast.LetRec(
            [ast.RecBinding("even", ev), ast.RecBinding("odd", od)],
            ast.Call(ast.Var("even"), [ast.Int(9)]),
        )
        s_value, _ = eval_source(s_root)
        self.assertEqual(s_value, 0)

    def test_higher_order_indirect_tail_calls(self):
        # loop 尾调用参数闭包 k，k=h（同组互递归成员）又尾调用 loop：
        # 经闭包间接的尾调用同样复用现场。
        loop_body = ast.If0(
            ast.Var("n"),
            ast.Int(0),
            ast.Call(ast.Var("k"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        )
        h_body = ast.If0(
            ast.Var("m"),
            ast.Int(0),
            ast.Call(
                ast.Var("loop"),
                [ast.Add(ast.Var("m"), ast.Int(-1)), ast.Var("h")],
            ),
        )
        root = ast.LetRec(
            [
                ast.RecBinding("loop", ast.Lambda(["n", "k"], loop_body)),
                ast.RecBinding("h", ast.Lambda(["m"], h_body)),
            ],
            ast.Call(ast.Var("loop"), [ast.Int(20_000), ast.Var("h")]),
        )
        value, events, stats = eval_target_tracked(convert(root), step_limit=10_000_000)
        self.assertEqual(value, 0)
        self.assertEqual(stats.peak_depth, 1)
        self.assertEqual(stats.tail_reuses, 20_000)

    def test_non_tail_calls_keep_nested_frames(self):
        # 普通调用行为不变：深度随嵌套增长，没有任何尾复用
        n = 30
        value, events, stats = eval_target_tracked(
            convert(nontail_loop_program(n)), step_limit=10_000
        )
        self.assertEqual(value, 0)
        self.assertEqual(stats.tail_reuses, 0)
        self.assertEqual(stats.calls, n + 1)
        self.assertEqual(stats.peak_depth, n + 1)
        self.assertEqual(stats.depth, 0)

    def test_tail_position_marks_only_true_tail_calls(self):
        # 递归调用在尾位置 -> tail=True；Add 实参里的调用 -> tail=False
        prog_tail = convert(tail_loop_program(3))
        tail_calls = list(iter_tcalls(prog_tail.main))
        self.assertTrue(tail_calls)
        self.assertTrue(all(c.tail for c in tail_calls[:-1]))
        # 主层发起的第一次调用不在函数体内，tail=False
        self.assertFalse(tail_calls[-1].tail)

        prog_non = convert(nontail_loop_program(3))
        non_calls = list(iter_tcalls(prog_non.main))
        self.assertTrue(non_calls)
        self.assertFalse(any(c.tail for c in non_calls))

    def test_captured_mutable_cell_survives_frame_reuse(self):
        # c 被 inc/get 闭包以同一个 Cell 捕获；尾循环在实参位置以普通调用
        # 执行 inc()（+1 再 -1 抵消），循环本身是尾调用。现场复用两万次后
        # 捕获的 Cell 仍存活、计数正确、副作用顺序与源一致。
        # 本语言没有 begin；用 if0(inc(), <不可达>, loop(n-1)) 表达
        # “先普通调用 inc() 读写共享 Cell，再尾调用 loop”的顺序：
        # inc() 返回 c>=1 永不为 0，故恒走 else（尾位置）分支。
        recurse = ast.If0(
            ast.Call(ast.Var("inc"), []),
            ast.Int(-1),  # 不可达（c 从 1 开始递增，永不为 0）
            ast.Call(
                ast.Var("loop"), [ast.Add(ast.Var("n"), ast.Int(-1))]
            ),
        )
        loop_body = ast.If0(ast.Var("n"), ast.Call(ast.Var("get"), []), recurse)

        def build(limit_n):
            return ast.Let(
                "c",
                ast.Int(0),
                ast.Let(
                    "inc",
                    ast.Lambda(
                        [], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))
                    ),
                    ast.Let(
                        "get",
                        ast.Lambda([], ast.Var("c")),
                        ast.LetRec(
                            [ast.RecBinding("loop", ast.Lambda(["n"], loop_body))],
                            ast.Call(ast.Var("loop"), [ast.Int(limit_n)]),
                        ),
                    ),
                ),
            )

        deep_n = 2_000
        value, events, stats = eval_target_tracked(
            convert(build(deep_n)), step_limit=10_000_000
        )
        # 每个非基例层执行一次 inc（共 deep_n 次）；最终进入 loop(0)
        # 基例的那次跳转也复用了现场（共 deep_n+1 次尾复用）。
        self.assertEqual(value, deep_n)
        self.assertEqual(len(events), deep_n)
        self.assertEqual(events[0], ("set", "c", 1))
        self.assertEqual(events[-1], ("set", "c", deep_n))
        self.assertEqual(stats.peak_depth, 2)  # loop 帧 + 实参位置的 inc/get 帧
        self.assertEqual(stats.tail_reuses, deep_n + 1)

        s_value, s_events = eval_source(build(50), step_limit=10_000)
        t_value, t_events, _ = eval_target_tracked(
            convert(build(50)), step_limit=10_000
        )
        self.assertEqual((s_value, s_events), (t_value, t_events))

    def test_captured_immutable_value_survives_frame_reuse(self):
        # k 是不可变 val 捕获；尾循环基例返回 k，证明旧现场的活数据
        # 已随闭包记录保留，没有被错误释放。
        body = ast.If0(
            ast.Var("n"),
            ast.Var("k"),
            ast.Call(ast.Var("loop"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        )
        root = ast.Let(
            "k",
            ast.Int(11),
            ast.LetRec(
                [ast.RecBinding("loop", ast.Lambda(["n"], body))],
                ast.Call(ast.Var("loop"), [ast.Int(10_000)]),
            ),
        )
        value, _, stats = eval_target_tracked(convert(root), step_limit=10_000_000)
        self.assertEqual(value, 11)
        self.assertEqual(stats.peak_depth, 1)

    def test_tail_call_errors_still_raised(self):
        # 尾位置上的调用非函数 / 元数不符，错误语义与普通调用一致
        bad_callee = ast.LetRec(
            [ast.RecBinding(
                "loop",
                ast.Lambda(
                    ["n"],
                    ast.If0(
                        ast.Var("n"),
                        ast.Int(0),
                        ast.Call(ast.Int(1), [ast.Var("n")]),
                    ),
                ),
            )],
            ast.Call(ast.Var("loop"), [ast.Int(3)]),
        )
        with self.assertRaises(errors.LangRuntimeError):
            eval_target_tracked(convert(bad_callee), step_limit=10_000)

        bad_arity = ast.LetRec(
            [ast.RecBinding(
                "loop",
                ast.Lambda(
                    ["n"],
                    ast.If0(
                        ast.Var("n"),
                        ast.Int(0),
                        ast.Call(ast.Var("loop"), [ast.Var("n"), ast.Int(1)]),
                    ),
                ),
            )],
            ast.Call(ast.Var("loop"), [ast.Int(3)]),
        )
        with self.assertRaises(errors.LangRuntimeError):
            eval_target_tracked(convert(bad_arity), step_limit=10_000)

    def test_step_limit_in_tail_chain_preserves_events(self):
        root = tail_loop_program(10_000)
        with self.assertRaises(errors.StepLimitError) as ctx:
            eval_target_tracked(convert(root), step_limit=100)
        self.assertEqual(ctx.exception.events, [])
        self.assertEqual(ctx.exception.limit, 100)


if __name__ == "__main__":
    unittest.main()

"""接口上限与错误语义：500 节点、10000 步、纯函数式失败不留部分状态。"""

import unittest

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source, eval_target


def _add_chain_fresh_leaves(n_adds):
    """n_adds 个 Add，右侧每次都是新 Int：节点数 = 1 + 2*n_adds（奇数）。"""
    node = ast.Int(0)
    for k in range(n_adds):
        node = ast.Add(node, ast.Int(k))
    return node


def _add_chain_started_from(start, n_adds):
    """在给定子树（计入其节点数）上叠加 n_adds 个 Add，每个 +2 节点。"""
    node = start
    for k in range(n_adds):
        node = ast.Add(node, ast.Int(k))
    return node


def _if0_ints():
    """三个 Int 叶的 if0：4 个节点（偶数）。"""
    return ast.If0(ast.Int(0), ast.Int(1), ast.Int(2))


def _wrap_lets(node, n):
    """套 n 层 let，每层带一个 Int 值：新增 2n 个节点。"""
    for k in range(n):
        node = ast.Let(f"z{k}", ast.Int(0), node)
    return node


class TestNodeLimit(unittest.TestCase):
    def test_500_boundary_accepted(self):
        # if0(4) + 148 adds(296) + 100 lets(200) = 500
        root = _wrap_lets(
            _add_chain_started_from(_if0_ints(), 148),
            100,
        )
        self.assertEqual(ast.count_nodes(root), 500)
        program = convert(root)  # 恰在上限，必须通过
        self.assertEqual(program.n_nodes, 500)
        sv, _ = eval_source(root)
        tv, _ = eval_target(program)
        self.assertEqual(sv, tv)

    def test_501_nodes_rejected(self):
        # 1 + 2*250 = 501
        root = _add_chain_fresh_leaves(250)
        self.assertEqual(ast.count_nodes(root), 501)
        with self.assertRaises(errors.NodeLimitError) as cm:
            convert(root)
        self.assertEqual(cm.exception.count, 501)
        self.assertEqual(cm.exception.limit, 500)


class TestStepLimit(unittest.TestCase):
    def _infinite_loop(self):
        return ast.LetRec(
            [
                ast.RecBinding(
                    "f",
                    ast.Lambda(["n"], ast.Call(ast.Var("f"), [ast.Var("n")])),
                )
            ],
            ast.Call(ast.Var("f"), [ast.Int(0)]),
        )

    def test_target_step_limit_10000(self):
        program = convert(self._infinite_loop())
        with self.assertRaises(errors.StepLimitError) as cm:
            eval_target(program)
        self.assertEqual(cm.exception.limit, 10_000)
        self.assertGreaterEqual(cm.exception.steps, 10_000)

    def test_source_step_limit_10000(self):
        with self.assertRaises(errors.StepLimitError) as cm:
            eval_source(self._infinite_loop())
        self.assertEqual(cm.exception.limit, 10_000)

    def test_custom_small_limit(self):
        program = convert(self._infinite_loop())
        with self.assertRaises(errors.StepLimitError) as cm:
            eval_target(program, step_limit=30)
        self.assertEqual(cm.exception.limit, 30)
        self.assertGreaterEqual(cm.exception.steps, 30)

    def test_step_limit_distinct_from_provable_failure(self):
        # 未绑定变量是确定性的静态失败，绝不能被报成预算耗尽
        with self.assertRaises(errors.UnboundVariableError):
            convert(ast.Var("nope"))
        # 运行期类型错误同样不是预算问题
        e = ast.Call(ast.Int(1), [])
        with self.assertRaises(errors.LangRuntimeError):
            eval_target(convert(e), step_limit=10_000)


class TestNoPartialState(unittest.TestCase):
    def test_failed_convert_leaves_no_program(self):
        # convert 失败只通过异常体现；调用方拿不到部分 Program。
        bad = ast.Lambda([], ast.Var("missing"))
        with self.assertRaises(errors.RecenvError):
            convert(ast.Call(bad, []))
        # 解释器状态是每次调用新建的；失败后另一次调用不受影响
        ok = ast.Add(ast.Int(1), ast.Int(2))
        v, events = eval_source(ok)
        self.assertEqual((v, events), (3, []))
        v2, events2 = eval_target(convert(ok))
        self.assertEqual((v2, events2), (3, []))

    def test_step_failure_preserves_prior_events(self):
        # 先发生副作用、再耗尽预算：异常必须携带已产生的事件序列
        e = ast.LetRec(
            [
                ast.RecBinding(
                    "f",
                    ast.Lambda(
                        ["n"],
                        ast.If0(
                            ast.Var("n"),
                            ast.Call(ast.Var("f"), [ast.Var("n")]),
                            ast.Add(
                                ast.Set("n", ast.Int(0)),
                                ast.Call(ast.Var("f"), [ast.Var("n")]),
                            ),
                        ),
                    ),
                )
            ],
            ast.Call(ast.Var("f"), [ast.Int(1)]),
        )
        program = convert(e)
        with self.assertRaises(errors.StepLimitError) as cm:
            eval_target(program, step_limit=100)
        self.assertEqual(cm.exception.events, [("set", "n", 0)])


if __name__ == "__main__":
    unittest.main()

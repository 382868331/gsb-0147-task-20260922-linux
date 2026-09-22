"""输入上限、步数预算与错误语义区分的测试。"""

import unittest

from closure_layout import parse, convert, run, run_source
from closure_layout import ast
from closure_layout.errors import (
    NodeLimitError, StepLimitError, UnboundVariableError,
    LangTypeError, ArityError, ValidationError,
)


class TestNodeLimit(unittest.TestCase):
    def _chain(self, n_wraps: int, base: ast.Node) -> ast.Node:
        node = base
        for _ in range(n_wraps):
            node = ast.add_(ast.Int(value=1), node)
        return node

    def test_500_nodes_accepted(self):
        # lambda 包裹整数 = 2 节点（偶）；每次 add 增加 2 节点，
        # 249 次后恰好 500。顶层为闭包不可运行，故这里只验证 convert。
        base = ast.lambda_((), ast.Int(value=0))  # 2 节点
        node = self._chain(249, base)
        self.assertEqual(ast.count_nodes(node), 500)
        convert(node)  # 不抛 NodeLimitError

    def test_501_nodes_rejected(self):
        # Int 起点 1 节点（奇）；250 次 add 后 1 + 250*2 = 501。
        node = self._chain(250, ast.Int(value=0))
        self.assertEqual(ast.count_nodes(node), 501)
        with self.assertRaises(NodeLimitError):
            convert(node)

    def test_source_ref_also_enforces_node_limit(self):
        node = self._chain(250, ast.Int(value=0))
        with self.assertRaises(NodeLimitError):
            run_source(node)


class TestStepBudget(unittest.TestCase):
    def test_self_recursion_exhausts_budget(self):
        src = "(letrec ((lp (lambda () (lp)))) (lp))"
        tree = parse(src)
        with self.assertRaises(StepLimitError):
            run(convert(tree))
        with self.assertRaises(StepLimitError):
            run_source(tree)

    def test_mutual_recursion_exhausts_budget(self):
        src = ("(letrec ((a (lambda () (b))) (b (lambda () (a)))) (a))")
        tree = parse(src)
        with self.assertRaises(StepLimitError):
            run(convert(tree))

    def test_budget_distinct_from_unbound(self):
        # 预算耗尽是 StepLimitError；未绑定是 UnboundVariableError，
        # 二者错误类型必须可区分。
        self.assertFalse(issubclass(StepLimitError, UnboundVariableError))
        with self.assertRaises(UnboundVariableError):
            convert(parse("missing"))

    def test_custom_smaller_budget(self):
        src = "(+ 1 (+ 2 3))"
        # 极小预算也应抛 StepLimitError 而非静默截断。
        with self.assertRaises(StepLimitError):
            run(convert(parse(src)), max_steps=1)

    def test_bad_max_steps_rejected(self):
        prog = convert(parse("1"))
        for bad in (0, -1, True, 1.5):
            with self.assertRaises((ValueError, TypeError)):
                run(prog, max_steps=bad)

    def test_within_budget_program_completes(self):
        src = "(+ 1 2)"
        result = run(convert(parse(src)), max_steps=10_000)
        self.assertEqual(result.value, 3)
        self.assertGreater(result.steps, 0)
        self.assertLessEqual(result.steps, 10_000)


class TestRuntimeErrorsParity(unittest.TestCase):
    def test_add_non_function_value_type_error(self):
        src = "(+ (if0 0 1 2) 0)"  # 正常
        self.assertEqual(run(convert(parse(src))).value, 1)
        bad = "(1 2)"  # 对整数发起调用
        with self.assertRaises(LangTypeError):
            run(convert(parse(bad)))
        with self.assertRaises(LangTypeError):
            run_source(parse(bad))

    def test_add_non_integer(self):
        # 把闭包当整数相加。
        src = "(+ (lambda () 1) 2)"
        with self.assertRaises(LangTypeError):
            run(convert(parse(src)))
        with self.assertRaises(LangTypeError):
            run_source(parse(src))

    def test_arity_mismatch(self):
        src = "((lambda (a b) (+ a b)) 1)"
        with self.assertRaises(ArityError):
            run(convert(parse(src)))
        with self.assertRaises(ArityError):
            run_source(parse(src))

    def test_if0_condition_must_be_int(self):
        src = "(if0 (lambda () 1) 0 1)"
        with self.assertRaises(LangTypeError):
            run(convert(parse(src)))
        with self.assertRaises(LangTypeError):
            run_source(parse(src))


class TestTopLevelMustBeInteger(unittest.TestCase):
    def test_top_level_lambda_rejected(self):
        # 顶层表达式求值为闭包：程序结果契约要求整数。
        with self.assertRaises(LangTypeError):
            run(convert(parse("(lambda () 1)")))


if __name__ == "__main__":
    unittest.main()

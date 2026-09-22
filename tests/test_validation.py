"""AST 与输入校验测试：可定位错误、拒绝 bool/NaN/Infinity、结构非法。"""

import math
import unittest

from closure_layout import ast
from closure_layout.ast import (
    int_, add_, var_, set_, if0_, lambda_, call_, let_, letrec_,
)
from closure_layout.errors import (
    ValidationError, LetrecFormError, NodeLimitError,
)
from closure_layout import parse


class TestLiteralValidation(unittest.TestCase):
    def test_reject_bool(self):
        with self.assertRaises(ValidationError):
            int_(True)
        with self.assertRaises(ValidationError):
            int_(False)

    def test_reject_float_nan_inf(self):
        for bad in (float("nan"), float("inf"), float("-inf"), 1.5, 0.0):
            with self.assertRaises(ValidationError):
                int_(bad)

    def test_reject_nan_message(self):
        with self.assertRaisesRegex(ValidationError, "NaN"):
            int_(float("nan"))

    def test_accept_ints_including_big_negative(self):
        self.assertEqual(int_(0).value, 0)
        self.assertEqual(int_(-(10**50)).value, -(10**50))

    def test_bad_pos(self):
        with self.assertRaises(ValidationError):
            int_(1, pos=(0, 1))
        with self.assertRaises(ValidationError):
            int_(1, pos=(1,))


class TestStructuralValidation(unittest.TestCase):
    def test_duplicate_lambda_params(self):
        node = lambda_(("a", "a"), var_("a"))
        with self.assertRaisesRegex(ValidationError, "重名"):
            ast.validate(node)

    def test_duplicate_letrec_names(self):
        lam = lambda_(("t",), int_(0))
        node = letrec_((("f", lam), ("f", lam)), var_("f"))
        with self.assertRaisesRegex(ValidationError, "letrec 绑定重名"):
            ast.validate(node)

    def test_letrec_rhs_must_be_lambda(self):
        # 直接用 AST 构造一个右值为整数的 letrec。
        node = ast.Letrec(bindings=(("f", ast.Int(value=1)),),  # type: ignore[arg-type]
                          body=ast.Var(name="f"))
        with self.assertRaises(LetrecFormError):
            ast.validate(node)

    def test_non_node_child_rejected(self):
        node = add_(1, int_(2))  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            ast.validate(node)

    def test_keyword_binding_name_rejected(self):
        with self.assertRaises(ValidationError):
            parse("(lambda (if0) 1)")

    def test_empty_program_and_multiple_forms(self):
        with self.assertRaisesRegex(ValidationError, "空程序"):
            parse("")
        with self.assertRaisesRegex(ValidationError, "顶层只允许一个"):
            parse("1 2")

    def test_unbalanced_parens(self):
        with self.assertRaises(ValidationError):
            parse("(+ 1 2")
        with self.assertRaises(ValidationError):
            parse("(+ 1 2))")

    def test_empty_parens(self):
        with self.assertRaises(ValidationError):
            parse("()")

    def test_error_carries_position(self):
        from closure_layout.errors import UnboundVariableError
        try:
            from closure_layout import convert
            convert(parse("(+ 1 missing)"))
        except UnboundVariableError as e:
            self.assertIsNotNone(e.line)
            self.assertIsNotNone(e.column)
            self.assertIn("line", str(e))
        else:  # pragma: no cover
            self.fail("应当抛出未绑定变量错误")

    def test_node_limit_boundary(self):
        # 构造 501 个节点：Int(0) + 250 次 add（每次 +2 节点）。
        node = ast.Int(value=0)
        for _ in range(250):
            node = add_(int_(1), node)
        self.assertEqual(ast.count_nodes(node), 501)
        with self.assertRaises(NodeLimitError):
            from closure_layout import convert
            convert(node)


if __name__ == "__main__":
    unittest.main()

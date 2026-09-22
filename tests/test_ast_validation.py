"""AST 构造与公开接口的输入校验、可定位错误。"""

import math
import unittest

from recenv import ast_nodes as ast
from recenv import convert, errors


class TestIntValidation(unittest.TestCase):
    def test_bool_rejected(self):
        with self.assertRaises(errors.ASTError):
            ast.Int(True)
        with self.assertRaises(errors.ASTError):
            ast.Int(False)

    def test_plain_int_ok(self):
        self.assertEqual(ast.Int(42).value, 42)
        self.assertEqual(ast.Int(-7).value, -7)

    def test_float_nan_inf_rejected(self):
        with self.assertRaises(errors.ASTError):
            ast.Int(float("nan"))
        with self.assertRaises(errors.ASTError):
            ast.Int(float("inf"))
        with self.assertRaises(errors.ASTError):
            ast.Int(float("-inf"))

    def test_float_integral_accepted_fraction_rejected(self):
        self.assertEqual(ast.Int(3.0).value, 3)
        with self.assertRaises(errors.ASTError):
            ast.Int(3.5)

    def test_non_number_rejected(self):
        with self.assertRaises(errors.ASTError):
            ast.Int("3")
        with self.assertRaises(errors.ASTError):
            ast.Int(None)

    def test_error_is_localizable(self):
        try:
            ast.Int(True, loc="L7")
        except errors.ASTError as e:
            self.assertTrue(str(e).startswith("L7:"), str(e))
        else:
            self.fail("expected ASTError")


class TestShapeValidation(unittest.TestCase):
    def test_var_name(self):
        with self.assertRaises(errors.ASTError):
            ast.Var(3)
        with self.assertRaises(errors.ASTError):
            ast.Var("")

    def test_lambda_duplicate_params(self):
        with self.assertRaises(errors.ASTError):
            ast.Lambda(["a", "a"], ast.Int(1))

    def test_lambda_params_type(self):
        with self.assertRaises(errors.ASTError):
            ast.Lambda("a", ast.Int(1))  # type: ignore[arg-type]

    def test_call_args_type(self):
        with self.assertRaises(errors.ASTError):
            ast.Call(ast.Lambda([], ast.Int(0)), ast.Int(1))  # type: ignore[arg-type]

    def test_letrec_empty(self):
        with self.assertRaises(errors.ASTError):
            ast.LetRec([], ast.Int(0))

    def test_letrec_duplicate_names(self):
        with self.assertRaises(errors.ASTError):
            ast.LetRec(
                [
                    ast.RecBinding("f", ast.Lambda([], ast.Int(1))),
                    ast.RecBinding("f", ast.Lambda([], ast.Int(2))),
                ],
                ast.Int(0),
            )

    def test_letrec_binding_must_be_lambda(self):
        with self.assertRaises(errors.ASTError):
            ast.RecBinding("f", ast.Int(1))
        with self.assertRaises(errors.ASTError):
            ast.LetRec(
                [ast.RecBinding("f", ast.Int(1), )],  # type: ignore[arg-type]
                ast.Int(0),
            )

    def test_subexpression_must_be_node(self):
        with self.assertRaises(errors.ASTError):
            ast.Add(1, ast.Int(2))  # type: ignore[arg-type]
        with self.assertRaises(errors.ASTError):
            ast.If0(ast.Int(0), ast.Int(1), "x")  # type: ignore[arg-type]

    def test_convert_rejects_non_expression(self):
        with self.assertRaises(errors.ASTError):
            convert("not an ast")  # type: ignore[arg-type]
        with self.assertRaises(errors.ASTError):
            convert(ast.RecBinding("f", ast.Lambda([], ast.Int(0))))


class TestLocatableStaticErrors(unittest.TestCase):
    def test_unbound_carries_loc(self):
        bad = ast.Lambda([], ast.Var("z", loc="L99"), loc="lam")
        try:
            convert(ast.Call(bad, (), loc="call"))
        except errors.UnboundVariableError as e:
            self.assertTrue(str(e).startswith("L99:"), str(e))
            self.assertEqual(e.name, "z")
        else:
            self.fail("expected UnboundVariableError")

    def test_unbound_set_carries_loc(self):
        node = ast.Let(
            "f",
            ast.Lambda([], ast.Set("q", ast.Int(1), loc="S3")),
            ast.Call(ast.Var("f"), ()),
        )
        with self.assertRaises(errors.UnboundVariableError) as cm:
            convert(node)
        self.assertTrue(str(cm.exception).startswith("S3:"))

    def test_set_letrec_name_is_static_error(self):
        node = ast.LetRec(
            [ast.RecBinding("f", ast.Lambda([], ast.Set("f", ast.Int(1), loc="setrec")))],
            ast.Int(0),
        )
        with self.assertRaises(errors.ScopeError) as cm:
            convert(node)
        self.assertIn("f", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

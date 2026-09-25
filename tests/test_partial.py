"""Partial 应用语义：前缀绑定、续应用、严格顺序与环境共享。"""

import unittest

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source, eval_target
from recenv import source_interp as si
from recenv import target_interp as ti


def run_both(root, step_limit=10_000):
    s_val, s_events = eval_source(root, step_limit=step_limit)
    program = convert(root)
    t_val, t_events = eval_target(program, step_limit=step_limit)
    s_steps = si._SrcInterp(step_limit)
    t_steps = ti._TargetInterp(program, step_limit)
    s_steps.eval(root, None)
    t_steps.eval(program.main, ())
    return (s_val, s_events, s_steps.steps), (t_val, t_events, t_steps.steps), program


class TestPartial(unittest.TestCase):
    def test_prefix_then_old_exact_call(self):
        root = ast.Let(
            "f",
            ast.Lambda(["a", "b"], ast.Add(ast.Var("a"), ast.Var("b"))),
            ast.Call(
                ast.Partial(ast.Var("f"), [ast.Int(40)]),
                [ast.Int(2)],
            ),
        )
        source, target, _ = run_both(root)
        self.assertEqual(source, target)
        self.assertEqual(source[0], 42)

    def test_zero_args_returns_closure_without_entering_body(self):
        root = ast.Let(
            "c",
            ast.Int(0),
            ast.Let(
                "p",
                ast.Partial(
                    ast.Lambda(["a", "b"], ast.Set("c", ast.Int(99))),
                    [],
                ),
                ast.Var("c"),
            ),
        )
        source, target, _ = run_both(root)
        self.assertEqual(source, target)
        self.assertEqual(source, (0, [], 6))

    def test_zero_arg_function_partial_can_be_called_later(self):
        root = ast.Let(
            "c",
            ast.Int(0),
            ast.Let(
                "p",
                ast.Partial(ast.Lambda([], ast.Set("c", ast.Int(99))), []),
                ast.Add(ast.Call(ast.Var("p"), []), ast.Var("c")),
            ),
        )
        source, target, _ = run_both(root)
        self.assertEqual(source, target)
        self.assertEqual(source[:2], (198, [("set", "c", 99)]))

    def test_zero_args_partial_keeps_existing_prefix(self):
        root = ast.Let(
            "f",
            ast.Lambda(["a", "b"], ast.Add(ast.Var("a"), ast.Var("b"))),
            ast.Partial(ast.Partial(ast.Var("f"), [ast.Int(40)]), []),
        )
        source = si._SrcInterp(10_000)
        source_closure = source.eval(root, None)
        self.assertEqual(source_closure.prefix, (40,))

        program = convert(root)
        target = ti._TargetInterp(program, 10_000)
        target_closure = target.eval(program.main, ())
        self.assertEqual(target_closure.prefix, (40,))

    def test_partial_closure_keeps_same_capture_record(self):
        root = ast.Let(
            "x",
            ast.Int(7),
            ast.Partial(
                ast.Lambda(["a", "b"], ast.Add(ast.Var("x"), ast.Var("a"))),
                [ast.Int(1)],
            ),
        )
        source = si._SrcInterp(10_000)
        source_closure = source.eval(root, None)
        self.assertIsInstance(source_closure, si.PartialClosure)
        self.assertIs(
            source_closure.function.env.resolve_box("x"),
            source_closure.function.env.names["x"],
        )

        program = convert(root)
        target = ti._TargetInterp(program, 10_000)
        with_frame = program.main
        partial_expr = with_frame.body
        chain = ((7,),)
        base_closure = target.eval(partial_expr.func, chain)
        partial_closure = target.partial_apply(base_closure, [1])
        self.assertIs(partial_closure.record, base_closure.record)
        self.assertEqual(partial_closure.prefix, (1,))
        self.assertIs(target.partial_apply(base_closure, []), base_closure)

        source_env = si.Scope(None)
        source_env.declare("x", si.Box(7))
        source_base = si.SrcClosure(("a", "b"), ast.Var("x"), source_env)
        self.assertIs(source.partial_apply(source_base, [], None), source_base)

    def test_bound_arguments_evaluate_once_even_when_closure_reused(self):
        root = ast.Let(
            "c",
            ast.Int(0),
            ast.Let(
                "f",
                ast.Lambda(["a", "b"], ast.Add(ast.Var("a"), ast.Var("b"))),
                ast.Let(
                    "p",
                    ast.Partial(ast.Var("f"), [ast.Set("c", ast.Int(10))]),
                    ast.Add(
                        ast.Call(ast.Var("p"), [ast.Int(1)]),
                        ast.Call(ast.Var("p"), [ast.Int(2)]),
                    ),
                ),
            ),
        )
        source, target, _ = run_both(root)
        self.assertEqual(source, target)
        self.assertEqual(source[:2], (23, [("set", "c", 10)]))

    def test_chained_partial_and_over_application(self):
        root = ast.Let(
            "f",
            ast.Lambda(
                ["a", "b"],
                ast.Lambda(
                    ["c", "d"],
                    ast.Add(
                        ast.Add(ast.Var("a"), ast.Var("b")),
                        ast.Add(ast.Var("c"), ast.Var("d")),
                    ),
                ),
            ),
            ast.Let(
                "p",
                ast.Partial(ast.Var("f"), [ast.Int(1)]),
                ast.Partial(ast.Var("p"), [ast.Int(2), ast.Int(3), ast.Int(4)]),
            ),
        )
        source, target, _ = run_both(root)
        self.assertEqual(source, target)
        self.assertEqual(source[0], 10)

    def test_prefix_closure_can_receive_more_than_remaining_args(self):
        root = ast.Let(
            "f",
            ast.Lambda(
                ["a", "b"],
                ast.Lambda(
                    ["c"],
                    ast.Add(ast.Add(ast.Var("a"), ast.Var("b")), ast.Var("c")),
                ),
            ),
            ast.Partial(
                ast.Partial(ast.Var("f"), [ast.Int(1)]),
                [ast.Int(2), ast.Int(3)],
            ),
        )
        source, target, _ = run_both(root)
        self.assertEqual(source, target)
        self.assertEqual(source[0], 6)

    def test_mutual_recursion_partial_keeps_shared_group(self):
        even = ast.Lambda(
            ["n"],
            ast.If0(
                ast.Var("n"),
                ast.Int(1),
                ast.Call(ast.Var("odd"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        )
        odd = ast.Lambda(
            ["n"],
            ast.If0(
                ast.Var("n"),
                ast.Int(0),
                ast.Call(ast.Var("even"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        )
        root = ast.LetRec(
            [ast.RecBinding("even", even), ast.RecBinding("odd", odd)],
            ast.Call(
                ast.Partial(ast.Var("even"), []),
                [ast.Int(7)],
            ),
        )
        source, target, program = run_both(root)
        self.assertEqual(source, target)
        self.assertEqual(source[0], 0)
        self.assertTrue(
            any(label.startswith("cell:odd#id") for label in program.layout["f0"])
        )

    def test_captured_cell_still_shared_through_partial(self):
        root = ast.Let(
            "c",
            ast.Int(0),
            ast.Let(
                "make",
                ast.Lambda(
                    ["a", "b"],
                    ast.Lambda(
                        ["d"],
                        ast.Set(
                            "c",
                            ast.Add(
                                ast.Var("c"),
                                ast.Add(
                                    ast.Add(ast.Var("a"), ast.Var("b")),
                                    ast.Var("d"),
                                ),
                            ),
                        ),
                    ),
                ),
                ast.Let(
                    "p",
                    ast.Partial(ast.Var("make"), [ast.Int(1), ast.Int(2)]),
                    ast.Add(
                        ast.Call(ast.Var("p"), [ast.Int(10)]),
                        ast.Call(ast.Var("p"), [ast.Int(20)]),
                    ),
                ),
            ),
        )
        source, target, _ = run_both(root)
        self.assertEqual(source, target)
        self.assertEqual(
            source[:2],
            (49, [("set", "c", 13), ("set", "c", 36)]),
        )

    def test_extra_application_checks_after_body_and_argument_effects(self):
        root = ast.Let(
            "c",
            ast.Int(0),
            ast.Partial(
                ast.Lambda(
                    ["a", "b"],
                    ast.Set("c", ast.Add(ast.Var("a"), ast.Var("b"))),
                ),
                [
                    ast.Set("c", ast.Int(1)),
                    ast.Set("c", ast.Int(2)),
                    ast.Set("c", ast.Int(3)),
                ],
            ),
        )
        with self.assertRaises(errors.LangRuntimeError):
            eval_source(root)
        with self.assertRaises(errors.LangRuntimeError):
            eval_target(convert(root))

        source = si._SrcInterp(10_000)
        with self.assertRaises(errors.LangRuntimeError):
            source.eval(root, None)
        target_program = convert(root)
        target = ti._TargetInterp(target_program, 10_000)
        with self.assertRaises(errors.LangRuntimeError):
            target.eval(target_program.main, ())
        self.assertEqual(
            source.events,
            [("set", "c", 1), ("set", "c", 2), ("set", "c", 3), ("set", "c", 3)],
        )
        self.assertEqual(source.events, target.events)

    def test_old_call_retains_exact_arity(self):
        root = ast.Let(
            "f",
            ast.Lambda(["a", "b"], ast.Add(ast.Var("a"), ast.Var("b"))),
            ast.Call(
                ast.Partial(ast.Var("f"), [ast.Int(1)]),
                [ast.Int(2), ast.Int(3)],
            ),
        )
        with self.assertRaises(errors.LangRuntimeError):
            eval_source(root)
        with self.assertRaises(errors.LangRuntimeError):
            eval_target(convert(root))


if __name__ == "__main__":
    unittest.main()

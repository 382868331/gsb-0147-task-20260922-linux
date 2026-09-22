"""额外结构性边界：深嵌套闭包的 depth 解析、嵌套 letrec、
递归组捕获外层不可变变量、三个闭包共享同一 cell。"""

import unittest

from recenv import ast_nodes as ast
from recenv import convert, eval_source, eval_target


def run_both(root):
    s_val, s_ev = eval_source(root)
    program = convert(root)
    t_val, t_ev = eval_target(program)
    return (s_val, s_ev), (t_val, t_ev), program


class TestNestedStructures(unittest.TestCase):
    def test_three_level_nested_closures(self):
        # let a = 1 in let b = 2 in
        # (() -> (() -> a + b + 7)())()
        e = ast.Let(
            "a", ast.Int(1),
            ast.Let(
                "b", ast.Int(2),
                ast.Call(
                    ast.Lambda(
                        [],
                        ast.Call(
                            ast.Lambda(
                                [],
                                ast.Add(
                                    ast.Add(ast.Var("a"), ast.Var("b")),
                                    ast.Int(7),
                                ),
                            ),
                            [],
                        ),
                    ),
                    [],
                ),
            ),
        )
        (sv, _), (tv, _), _ = run_both(e)
        self.assertEqual((sv, tv), (10, 10))

    def test_three_closures_share_one_cell(self):
        # inc / dec / get 三个闭包共享 c
        e = ast.Let(
            "c", ast.Int(10),
            ast.Let(
                "inc", ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))),
                ast.Let(
                    "dec", ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(-1)))),
                    ast.Let(
                        "get", ast.Lambda([], ast.Var("c")),
                        ast.Add(
                            ast.Call(ast.Var("inc"), []),
                            ast.Add(
                                ast.Call(ast.Var("inc"), []),
                                ast.Add(
                                    ast.Call(ast.Var("dec"), []),
                                    ast.Call(ast.Var("get"), []),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        (sv, se), (tv, te), prog = run_both(e)
        # c: 10->11->12->11; inc 返回 11,12; dec 返回 11; get 11 => 45
        self.assertEqual(sv, 45)
        self.assertEqual(
            se,
            [("set", "c", 11), ("set", "c", 12), ("set", "c", 11)],
        )
        self.assertEqual((sv, se), (tv, te))
        # 三个函数的捕获槽都标注同一个绑定 id
        for fid, fn in prog.functions.items():
            self.assertEqual(fn.n_capture, 1)

    def test_nested_letrec_groups(self):
        # 外层 iszero 组，内层 fib-ish 组；内层引用外层函数
        iz = ast.Lambda(["n"], ast.If0(ast.Var("n"), ast.Int(1), ast.Int(0)))
        nz = ast.Lambda(["n"], ast.If0(ast.Var("n"), ast.Int(0), ast.Int(1)))
        outer = ast.LetRec(
            [ast.RecBinding("iszero", iz), ast.RecBinding("nonzero", nz)],
            ast.LetRec(
                [
                    ast.RecBinding(
                        "loop",
                        ast.Lambda(
                            ["n"],
                            ast.If0(
                                ast.Var("n"),
                                ast.Int(0),
                                ast.Add(
                                    ast.Call(ast.Var("nonzero"), [ast.Var("n")]),
                                    ast.Call(
                                        ast.Var("loop"),
                                        [ast.Add(ast.Var("n"), ast.Int(-1))],
                                    ),
                                ),
                            ),
                        ),
                    )
                ],
                ast.Call(ast.Var("loop"), [ast.Int(5)]),
            ),
        )
        (sv, _), (tv, _), _ = run_both(outer)
        # nonzero(k)=1 for k=5..1，求和为 5
        self.assertEqual((sv, tv), (5, 5))

    def test_rec_group_captures_outer_immutable(self):
        # 外层 k 不可变；递归函数把 k 加入结果，证明捕获槽是 val 而非 cell
        e = ast.Let(
            "k", ast.Int(100),
            ast.LetRec(
                [
                    ast.RecBinding(
                        "f",
                        ast.Lambda(
                            ["n"],
                            ast.If0(
                                ast.Var("n"),
                                ast.Var("k"),
                                ast.Add(
                                    ast.Int(1),
                                    ast.Call(
                                        ast.Var("f"),
                                        [ast.Add(ast.Var("n"), ast.Int(-1))],
                                    ),
                                ),
                            ),
                        ),
                    )
                ],
                ast.Call(ast.Var("f"), [ast.Int(3)]),
            ),
        )
        (sv, _), (tv, _), prog = run_both(e)
        self.assertEqual((sv, tv), (103, 103))
        # f 的捕获槽恰好是 val:k
        fid = next(iter(prog.functions))
        self.assertIn("val:k", prog.layout[fid][0])

    def test_closure_passed_as_argument_keeps_cell(self):
        # inc 作为参数传入 twice；inc 修改外层 c，两次调用共享
        e = ast.Let(
            "c", ast.Int(0),
            ast.Let(
                "inc", ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))),
                ast.Let(
                    "twice",
                    ast.Lambda(
                        ["g"],
                        ast.Add(ast.Call(ast.Var("g"), []), ast.Call(ast.Var("g"), [])),
                    ),
                    ast.Add(
                        ast.Call(ast.Var("twice"), [ast.Var("inc")]),
                        ast.Var("c"),
                    ),
                ),
            ),
        )
        (sv, se), (tv, te), _ = run_both(e)
        # inc()=1, inc()=2, 和=3；c 最终=2；总计=5
        self.assertEqual((sv, se), (5, [("set", "c", 1), ("set", "c", 2)]))
        self.assertEqual((sv, se), (tv, te))

    def test_if0_branch_not_taken_not_evaluated(self):
        # 未选中的分支里放一个未绑定变量：不应报错（静态仍检测？）
        # 解析器做整树作用域检查，未绑定变量即使在死分支也静态拒绝；
        # 因此这里放运行期错误（调用整数），验证不求值该死分支。
        e = ast.If0(
            ast.Int(0),
            ast.Int(42),
            ast.Call(ast.Int(1), []),
        )
        (sv, _), (tv, _), _ = run_both(e)
        self.assertEqual((sv, tv), (42, 42))


class TestBindingIdOrdering(unittest.TestCase):
    def test_free_vars_ordered_by_id_not_name(self):
        # z 先声明（ID 小），a 后声明（ID 大）；
        # 字母序为 a,z，但捕获槽必须按绑定 ID 为 z,a。
        e = ast.Let(
            "z", ast.Int(1),
            ast.Let(
                "a", ast.Int(2),
                ast.Call(ast.Lambda([], ast.Add(ast.Var("z"), ast.Var("a"))), []),
            ),
        )
        (sv, _), (tv, _), prog = run_both(e)
        self.assertEqual((sv, tv), (3, 3))
        fid = next(iter(prog.functions))
        self.assertTrue(prog.layout[fid][0].startswith("val:z#id"))
        self.assertTrue(prog.layout[fid][1].startswith("val:a#id"))

    def test_letrec_name_shadows_outer_same_name(self):
        # 外层 f=7（整数）；内层 letrec 组同名 f 是递归函数，两者不能混淆。
        rec_body = ast.If0(
            ast.Var("n"),
            ast.Int(40),
            ast.Add(
                ast.Call(ast.Var("f"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
                ast.Int(1),
            ),
        )
        group = ast.LetRec(
            [ast.RecBinding("f", ast.Lambda(["n"], rec_body))],
            ast.Call(ast.Var("f"), [ast.Int(2)]),
        )
        e = ast.Let("f", ast.Int(7), ast.Add(ast.Var("f"), group))
        (sv, _), (tv, _), _ = run_both(e)
        # f(0)=40, f(1)=41, f(2)=42；外层整数 f=7 不受遮蔽影响 => 49
        self.assertEqual(sv, 49)
        self.assertEqual(sv, tv)


if __name__ == "__main__":
    unittest.main()

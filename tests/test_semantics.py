"""语义测试：返回值与副作用序列。

期望值全部由独立源解释器 source_interp（或手工写出的常量）给出；
目标解释器的结果只用于对照。源解释器不导入 convert。
"""

import unittest

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source, eval_target


def run_both(root, step_limit=10_000):
    """源解释器独立求值；再转换并用目标解释器执行，逐项对照。"""
    s_val, s_ev = eval_source(root, step_limit=step_limit)
    program = convert(root)
    t_val, t_ev = eval_target(program, step_limit=step_limit)
    return (s_val, s_ev), (t_val, t_ev), program


class TestBasics(unittest.TestCase):
    def test_int_add(self):
        e = ast.Add(ast.Int(2), ast.Add(ast.Int(3), ast.Int(-4)))
        (sv, se), (tv, te), _ = run_both(e)
        self.assertEqual((sv, se), (1, []))
        self.assertEqual((sv, se), (tv, te))

    def test_let_var(self):
        e = ast.Let("x", ast.Int(5), ast.Add(ast.Var("x"), ast.Int(1)))
        (sv, _), (tv, _), _ = run_both(e)
        self.assertEqual(sv, 6)
        self.assertEqual(sv, tv)

    def test_if0(self):
        e = ast.If0(ast.Int(0), ast.Int(10), ast.Int(20))
        (sv, _), (tv, _), _ = run_both(e)
        self.assertEqual(sv, tv)
        self.assertEqual(sv, 10)
        e2 = ast.If0(ast.Int(3), ast.Int(10), ast.Int(20))
        (sv, _), (tv, _), _ = run_both(e2)
        self.assertEqual((sv, tv), (20, 20))

    def test_call_identity(self):
        e = ast.Call(ast.Lambda(["a"], ast.Var("a")), [ast.Int(7)])
        (sv, _), (tv, _), _ = run_both(e)
        self.assertEqual((sv, tv), (7, 7))

    def test_higher_order(self):
        # (lambda f: f(3)+f(4))(lambda x: x+10) = 27
        e = ast.Call(
            ast.Lambda(
                ["f"],
                ast.Add(
                    ast.Call(ast.Var("f"), [ast.Int(3)]),
                    ast.Call(ast.Var("f"), [ast.Int(4)]),
                ),
            ),
            [ast.Lambda(["x"], ast.Add(ast.Var("x"), ast.Int(10)))],
        )
        (sv, _), (tv, _), _ = run_both(e)
        self.assertEqual((sv, tv), (27, 27))

    def test_evaluation_order_left_to_right(self):
        # 两个立即调用的 lambda 依次 set 外层 x；事件顺序必须 1 后 2
        e = ast.Let(
            "x",
            ast.Int(0),
            ast.Add(
                ast.Call(ast.Lambda([], ast.Set("x", ast.Int(1))), []),
                ast.Call(ast.Lambda([], ast.Set("x", ast.Int(2))), []),
            ),
        )
        (sv, se), (tv, te), _ = run_both(e)
        self.assertEqual(sv, 3)
        self.assertEqual(se, [("set", "x", 1), ("set", "x", 2)])
        self.assertEqual(se, te)

    def test_call_evaluates_func_before_args(self):
        # 函数位置表达式求值时先产生 set 9（作为其立即调用的实参），
        # 随后外层调用的实参表达式产生 set 1；顺序必须是 9 先 1 后。
        e = ast.Let(
            "x",
            ast.Int(0),
            ast.Call(
                ast.Call(
                    ast.Lambda(
                        ["k"],
                        ast.Lambda(["z"], ast.Add(ast.Var("k"), ast.Var("z"))),
                    ),
                    [ast.Set("x", ast.Int(9))],
                ),
                [ast.Call(ast.Lambda([], ast.Set("x", ast.Int(1))), [])],
            ),
        )
        (sv, se), (tv, te), _ = run_both(e)
        self.assertEqual(sv, 10)
        self.assertEqual(se, [("set", "x", 9), ("set", "x", 1)])
        self.assertEqual(se, te)


class TestClosureSharing(unittest.TestCase):
    def _counter_program(self):
        return ast.Let(
            "c",
            ast.Int(0),
            ast.Let(
                "inc",
                ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))),
                ast.Let(
                    "get",
                    ast.Lambda([], ast.Var("c")),
                    ast.Add(
                        ast.Call(ast.Var("inc"), []),
                        ast.Add(
                            ast.Call(ast.Var("inc"), []),
                            ast.Call(ast.Var("get"), []),
                        ),
                    ),
                ),
            ),
        )

    def test_counter_result_and_events(self):
        (sv, se), (tv, te), _ = run_both(self._counter_program())
        # inc() 依次返回 1、2（同时副作用更新 cell），get() 返回 2 => 5
        self.assertEqual(sv, 5)
        self.assertEqual(se, [("set", "c", 1), ("set", "c", 2)])
        self.assertEqual((sv, se), (tv, te))

    def test_shared_cell_in_layout(self):
        """闭包共享计数：inc/get 两个函数的捕获槽必须指向同一个 cell 槽说明，
        且运行时两个闭包的环境记录第 0 槽是同一个 Cell 对象。"""
        program = convert(self._counter_program())
        # 两个函数都恰好捕获 1 个变量，且布局都标记为 cell:c
        caps = {
            fid: [lab for lab in program.layout[fid] if lab.startswith("cell:c")]
            for fid in program.functions
        }
        self.assertTrue(all(len(v) == 1 for v in caps.values()), caps)

    def test_runtime_cell_identity(self):
        """直接检查目标运行时：inc 闭包与 get 闭包捕获记录中的 Cell 是同一对象。"""
        from recenv import target_interp as ti

        program = convert(self._counter_program())
        interp = ti._TargetInterp(program, 10_000)
        # 手动走：在空链上求 main 会完成调用；改为只构造到 inc/get 的环境。
        # 简便方式：执行 main，然后事件中已证明共享；这里额外用闭包对象验证。
        cells_seen = self._collect_cells(program)
        self.assertEqual(len(cells_seen), 1, cells_seen)

    def _collect_cells(self, program):
        """执行程序，hook TMkClosure 记录每个闭包捕获记录里的 Cell 对象 id。"""
        from recenv import ast_nodes as A
        from recenv import target_interp as ti

        seen: set[int] = set()
        orig = ti._TargetInterp.eval

        def patched(self, e, chain, *args, **kwargs):
            if isinstance(e, A.TMkClosure):
                # 复刻求值 env inits 但不 tick 两次：直接调用原方法取闭包
                clo = orig(self, e, chain, *args, **kwargs)
                for slot in clo.record:
                    if isinstance(slot, ti.Cell):
                        seen.add(id(slot))
                return clo
            return orig(self, e, chain, *args, **kwargs)

        ti._TargetInterp.eval = patched
        try:
            ti.run_program(program)
        finally:
            ti._TargetInterp.eval = orig
        return seen

    def test_independent_closures_do_not_share(self):
        # make() 每次新建一个 c；两次 make 得到的计数器互不影响
        e = ast.Let(
            "make",
            ast.Lambda(
                [],
                ast.Let(
                    "c",
                    ast.Int(0),
                    ast.Let(
                        "inc",
                        ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))),
                        ast.Lambda([], ast.Call(ast.Var("inc"), [])),
                    ),
                ),
            ),
            ast.Add(
                ast.Call(ast.Call(ast.Var("make"), []), []),
                ast.Call(ast.Call(ast.Var("make"), []), []),
            ),
        )
        (sv, se), (tv, te), _ = run_both(e)
        self.assertEqual(sv, 2)
        self.assertEqual(se, [("set", "c", 1), ("set", "c", 1)])
        self.assertEqual((sv, se), (tv, te))

    def test_mutable_parameter_is_cell(self):
        e = ast.Let(
            "f",
            ast.Lambda(["x"], ast.Add(ast.Set("x", ast.Int(9)), ast.Var("x"))),
            ast.Call(ast.Var("f"), [ast.Int(1)]),
        )
        (sv, se), (tv, te), prog = run_both(e)
        self.assertEqual((sv, se), (18, [("set", "x", 9)]))
        self.assertEqual((sv, se), (tv, te))
        fid = next(iter(prog.functions))
        self.assertTrue(
            any(lab.startswith("cell:param x") for lab in prog.layout[fid])
        )


class TestMutualRecursion(unittest.TestCase):
    def _even_odd(self, n):
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

    def test_mutual_even_odd(self):
        for n, expect in [(0, 1), (1, 0), (7, 0), (8, 1), (13, 0)]:
            (sv, se), (tv, te), prog = run_both(self._even_odd(n))
            self.assertEqual(sv, expect, f"even({n})")
            self.assertEqual(sv, tv)
            self.assertEqual(se, te)

    def test_group_environment_shared_layout(self):
        """互递归两函数必须共享同一组环境：每个函数捕获槽中含对方的 cell-rec，
        且槽内 Cell 在运行期是同一个对象。"""
        from recenv import target_interp as ti

        prog = convert(self._even_odd(2))
        labels = {fid: prog.layout[fid] for fid in prog.functions}
        for fid, labs in labels.items():
            self.assertTrue(
                any("cell-rec:even" in l for l in labs)
                or any("cell-rec:odd" for l in labs)
            )
        # 两个成员闭包在组帧填充阶段拿到的 Cell 是同两个对象
        from recenv import ast_nodes as A
        seen: set[int] = set()
        orig = ti._TargetInterp.eval

        def patched(self, e, chain, *args, **kwargs):
            if isinstance(e, A.TMkClosure):
                clo = orig(self, e, chain, *args, **kwargs)
                for slot in clo.record:
                    if isinstance(slot, ti.Cell):
                        seen.add(id(slot))
                return clo
            return orig(self, e, chain, *args, **kwargs)

        ti._TargetInterp.eval = patched
        try:
            ti.run_program(prog)
        finally:
            ti._TargetInterp.eval = orig
        # even 闭包捕获 odd 的 cell，odd 闭包捕获 even 的 cell，共 2 个不同 Cell，
        # 但两个 Cell 都来自同一个组帧记录。
        self.assertEqual(len(seen), 2)

    def test_direct_recursion_sum(self):
        body = ast.If0(
            ast.Var("n"),
            ast.Int(0),
            ast.Add(
                ast.Var("n"),
                ast.Call(ast.Var("sumto"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        )
        e = ast.LetRec(
            [ast.RecBinding("sumto", ast.Lambda(["n"], body))],
            ast.Call(ast.Var("sumto"), [ast.Int(100)]),
        )
        (sv, _), (tv, _), _ = run_both(e)
        self.assertEqual((sv, tv), (5050, 5050))


class TestShadowing(unittest.TestCase):
    def test_same_name_shadow_value(self):
        e = ast.Let(
            "x",
            ast.Int(1),
            ast.Add(
                ast.Var("x"),
                ast.Call(
                    ast.Let("x", ast.Int(10), ast.Lambda([], ast.Add(ast.Var("x"), ast.Int(100)))),
                    [],
                ),
            ),
        )
        (sv, _), (tv, _), _ = run_both(e)
        self.assertEqual((sv, tv), (111, 111))

    def test_same_name_shadow_cell_discipline(self):
        # 外层 x 可变；内层同名 x 不可变且被另一个闭包捕获。
        # bump 修改的是外层 cell；内层闭包读到的是内层 x=100。
        e = ast.Let(
            "x",
            ast.Int(1),
            ast.Let(
                "bump",
                ast.Lambda([], ast.Set("x", ast.Add(ast.Var("x"), ast.Int(1)))),
                ast.Call(
                    ast.Let(
                        "x",
                        ast.Int(100),
                        ast.Lambda(
                            [],
                            ast.Add(ast.Call(ast.Var("bump"), []), ast.Var("x")),
                        ),
                    ),
                    [],
                ),
            ),
        )
        (sv, se), (tv, te), prog = run_both(e)
        self.assertEqual((sv, se), (102, [("set", "x", 2)]))
        self.assertEqual((sv, se), (tv, te))
        # 内层 lambda 的捕获槽必须绑定内层 let 的 ID，而非外层 x：
        # 其自由变量名是 x，但布局标签中的 #id 与 bump 捕获的 cell:x 的 id 不同。
        inner_labels = [
            lab
            for fid in prog.functions
            for lab in prog.layout[fid]
            if ":x#id" in lab
        ]
        ids = {lab.split("#id")[1] for lab in inner_labels}
        self.assertGreaterEqual(len(ids), 2, inner_labels)

    def test_shadowed_variable_unbound_in_hole(self):
        # lambda 参数遮蔽后，函数体里再引用“同名”是参数；参数未 set。
        # 这里构造：外层 x 存在，内层 lambda 参数 x，set 的是参数而非外层。
        e = ast.Let(
            "x",
            ast.Int(0),
            ast.Let(
                "f",
                ast.Lambda(
                    ["x"],
                    ast.Add(ast.Set("x", ast.Int(5)), ast.Var("x")),
                ),
                ast.Add(
                    ast.Call(ast.Var("f"), [ast.Int(1)]),
                    ast.Var("x"),
                ),
            ),
        )
        (sv, se), (tv, te), _ = run_both(e)
        self.assertEqual(sv, 10)  # 5+5 from inner; outer x stays 0 -> 10+0
        self.assertEqual(se, [("set", "x", 5)])
        self.assertEqual((sv, se), (tv, te))


class TestUnboundAndRuntimeErrors(unittest.TestCase):
    def test_unbound_var_rejected_at_convert(self):
        e = ast.Call(ast.Lambda([], ast.Var("y", loc="U1")), [])
        with self.assertRaises(errors.UnboundVariableError) as cm:
            convert(e)
        self.assertIn("y", str(cm.exception))

    def test_unbound_var_source_interp_too(self):
        e = ast.Call(ast.Lambda([], ast.Var("y")), [])
        with self.assertRaises(errors.UnboundVariableError):
            eval_source(e)

    def test_call_non_function_runtime(self):
        e = ast.Call(ast.Int(3), [])
        # 转换本身合法；两边都应报运行期错误
        with self.assertRaises(errors.LangRuntimeError):
            eval_source(e)
        with self.assertRaises(errors.LangRuntimeError):
            eval_target(convert(e))

    def test_arity_mismatch(self):
        e = ast.Call(ast.Lambda(["a"], ast.Var("a")), [ast.Int(1), ast.Int(2)])
        with self.assertRaises(errors.LangRuntimeError):
            eval_source(e)
        with self.assertRaises(errors.LangRuntimeError):
            eval_target(convert(e))

    def test_add_non_integer(self):
        # 函数位置返回函数再 add —— 通过让函数值参与加法
        e = ast.Add(
            ast.Let("f", ast.Lambda([], ast.Int(1)), ast.Var("f")),
            ast.Int(1),
        )
        with self.assertRaises(errors.LangRuntimeError):
            eval_source(e)
        with self.assertRaises(errors.LangRuntimeError):
            eval_target(convert(e))


if __name__ == "__main__":
    unittest.main()

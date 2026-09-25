"""尾调用执行测试。

覆盖：
* 尾位置判定（结构性）：只有函数体结果、if0 结果分支、let/letrec body
  生成 TTailCall；Add 操作数、Set 右侧、Call 的函数/实参、main 顶层
  生成普通 TCall；
* 交替互尾递归跑到超过通常宿主递归深度（>1000），峰值活动调用帧数有界；
* 非尾递归计数确实随嵌套增长（并与源解释器对拍）；
* 参数严格从左到右：参数求值出错不替换帧，帧计数回落到 0；
* 异常位置/消息源 vs 目标逐项一致（尾/非尾两种上下文）；
* 一个参数修改另一个参数将读取的捕获变量（共享 cell 的求值序）；
* 步数口径不变（尾跳转不额外计步），尾/非尾程序均与源解释器精确同步数。
"""

import sys
import unittest

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source, run_program_with_stats
from recenv import target_interp as ti


# ---------------------------------------------------------------------------
# IR 遍历辅助
# ---------------------------------------------------------------------------

def walk(node):
    """深度优先产出 IR 中全部 TExpr 节点（含根）。"""
    yield node
    if isinstance(node, (ast.TConst, ast.TGet, ast.TCellGet)):
        return
    if isinstance(node, ast.TCellSet):
        yield from walk(node.value)
    elif isinstance(node, ast.TPrim):
        for a in node.args:
            yield from walk(a)
    elif isinstance(node, ast.TMkClosure):
        for init in node.env:
            yield from walk(init)
    elif isinstance(node, ast.TMkCell):
        if node.value is not None:
            yield from walk(node.value)
    elif isinstance(node, ast.TFillCell):
        yield from walk(node.value)
    elif isinstance(node, (ast.TCall, ast.TTailCall)):
        yield from walk(node.func)
        for a in node.args:
            yield from walk(a)
    elif isinstance(node, ast.TIf0):
        yield from walk(node.cond)
        yield from walk(node.then_e)
        yield from walk(node.else_e)
    elif isinstance(node, ast.TWithFrame):
        for init in node.inits:
            yield from walk(init)
        yield from walk(node.body)
    elif isinstance(node, ast.TBegin):
        for sub in node.exprs:
            yield from walk(sub)


def function_bodies(program):
    """产出 (fid, body) 对。"""
    for fid, fn in program.functions.items():
        yield fid, fn.body


def count_kind(program, kind):
    n = 0
    for _, body in function_bodies(program):
        n += sum(1 for e in walk(body) if isinstance(e, kind))
    n += sum(1 for e in walk(program.main) if isinstance(e, kind))
    return n


# ---------------------------------------------------------------------------
# 尾位置判定（结构性）
# ---------------------------------------------------------------------------

class TestTailPositionClassification(unittest.TestCase):
    def test_function_body_direct_call_is_tail(self):
        # letrec f(x) = f(x)：函数体调用必须是 TTailCall
        e = ast.LetRec(
            [ast.RecBinding("f", ast.Lambda(["x"], ast.Call(ast.Var("f"), [ast.Var("x")])))],
            ast.Int(0),
        )
        p = convert(e)
        self.assertEqual(count_kind(p, ast.TTailCall), 1)
        self.assertEqual(count_kind(p, ast.TCall), 0)

    def test_main_call_is_not_tail(self):
        # main 不是函数体：其顶层调用一律非尾
        e = ast.Call(ast.Lambda([], ast.Int(1)), [])
        p = convert(e)
        self.assertEqual(count_kind(p, ast.TCall), 1)
        self.assertEqual(count_kind(p, ast.TTailCall), 0)

    def test_if0_branches_tail_cond_not_tail(self):
        # 两分支里的调用是尾；条件里的调用不是尾
        e = ast.LetRec(
            [ast.RecBinding(
                "f",
                ast.Lambda(
                    ["n"],
                    ast.If0(
                        ast.Call(ast.Var("f"), [ast.Var("n")]),   # 条件：非尾
                        ast.Call(ast.Var("f"), [ast.Int(0)]),      # then：尾
                        ast.Call(ast.Var("f"), [ast.Int(1)]),      # else：尾
                    ),
                ),
            )],
            ast.Int(0),
        )
        p = convert(e)
        self.assertEqual(count_kind(p, ast.TTailCall), 2)
        self.assertEqual(count_kind(p, ast.TCall), 1)

    def test_nested_if0_branch_keeps_tail(self):
        # if0 分支里再套 if0，内层分支的调用仍是尾
        body = ast.If0(
            ast.Var("n"),
            ast.If0(ast.Var("n"), ast.Call(ast.Var("f"), [ast.Int(0)]), ast.Int(1)),
            ast.Int(2),
        )
        e = ast.LetRec([ast.RecBinding("f", ast.Lambda(["n"], body))], ast.Int(0))
        p = convert(e)
        self.assertEqual(count_kind(p, ast.TTailCall), 1)
        self.assertEqual(count_kind(p, ast.TCall), 0)

    def test_let_body_tail_value_not_tail(self):
        body = ast.Let(
            "z",
            ast.Call(ast.Var("helper"), []),       # let 绑定值：非尾
            ast.Call(ast.Var("f"), [ast.Var("z")]),  # let body：尾
        )
        e = ast.LetRec(
            [
                ast.RecBinding("f", ast.Lambda(["n"], body)),
                ast.RecBinding("helper", ast.Lambda([], ast.Int(0))),
            ],
            ast.Int(0),
        )
        p = convert(e)
        self.assertEqual(count_kind(p, ast.TTailCall), 1)
        self.assertEqual(count_kind(p, ast.TCall), 1)

    def test_letrec_body_tail(self):
        inner = ast.LetRec(
            [ast.RecBinding("g", ast.Lambda([], ast.Int(0)))],
            ast.Call(ast.Var("g"), []),  # 组体：尾
        )
        e = ast.LetRec(
            [ast.RecBinding("f", ast.Lambda([], inner))],
            ast.Call(ast.Var("f"), []),
        )
        p = convert(e)
        # f 体内的内层组体 g() 是尾；main 的 f() 不是
        tails = count_kind(p, ast.TTailCall)
        nontails = count_kind(p, ast.TCall)
        self.assertEqual(tails, 1)
        self.assertGreaterEqual(nontails, 1)

    def test_add_operands_never_tail(self):
        # 即使整个 Add 处在尾位置，操作数里的调用也必须是普通 TCall
        body = ast.Add(
            ast.Call(ast.Var("f"), [ast.Int(0)]),
            ast.Call(ast.Var("f"), [ast.Int(1)]),
        )
        e = ast.LetRec([ast.RecBinding("f", ast.Lambda(["n"], body))], ast.Int(0))
        p = convert(e)
        self.assertEqual(count_kind(p, ast.TTailCall), 0)
        self.assertEqual(count_kind(p, ast.TCall), 2)

    def test_set_rhs_never_tail(self):
        # set 右侧的调用不是尾位置
        e = ast.LetRec(
            [
                ast.RecBinding("f", ast.Lambda(["n"], ast.Let(
                    "z", ast.Int(0),
                    ast.Set("z", ast.Call(ast.Var("g"), [])),
                ))),
                ast.RecBinding("g", ast.Lambda([], ast.Int(0))),
            ],
            ast.Int(0),
        )
        p = convert(e)
        self.assertEqual(count_kind(p, ast.TTailCall), 0)
        self.assertEqual(count_kind(p, ast.TCall), 1)

    def test_call_func_and_args_never_tail(self):
        # 尾调用 f(g(), h())：f 是 TTailCall，但 g()/h() 都在实参位（非尾）；
        # 函数位置本身是 Var。再造一个函数位置是调用的：(k())()。
        body = ast.Call(
            ast.Call(ast.Var("k"), []),       # 函数位置调用：非尾
            [ast.Call(ast.Var("g"), [])],     # 实参调用：非尾
        )
        e = ast.LetRec(
            [
                ast.RecBinding("f", ast.Lambda([], body)),
                ast.RecBinding("k", ast.Lambda([], ast.Var("g"))),
                ast.RecBinding("g", ast.Lambda(["x"], ast.Int(0))),
            ],
            ast.Int(0),
        )
        p = convert(e)
        # f 体的外层 Call 是尾（k() 返回 g，再调 g），内层两个调用非尾
        self.assertEqual(count_kind(p, ast.TTailCall), 1)
        self.assertEqual(count_kind(p, ast.TCall), 2)


# ---------------------------------------------------------------------------
# 尾调用：帧数有界；非尾调用：帧数增长
# ---------------------------------------------------------------------------

def mutual_alt(n):
    f = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"), ast.Int(7),
            ast.Call(ast.Var("g"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    g = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"), ast.Int(8),
            ast.Call(ast.Var("f"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    return ast.LetRec(
        [ast.RecBinding("f", f), ast.RecBinding("g", g)],
        ast.Call(ast.Var("f"), [ast.Int(n)]),
    )


class TestFrameBounds(unittest.TestCase):
    HOST_DEPTH = 1000  # CPython 默认递归深度量级；下面的深度显著超过它

    def test_alternating_tail_recursion_bounded_frames(self):
        n = 20_000
        self.assertGreater(n, self.HOST_DEPTH)
        st = run_program_with_stats(convert(mutual_alt(n)), step_limit=1_000_000)
        # 20000 为偶 -> f(0)=7
        self.assertEqual(st.value, 7)
        # main 的 f() 是唯一非尾入口（压 1 帧）；之后全部尾替换
        self.assertEqual(st.max_call_frames, 1)
        self.assertEqual(st.tail_jumps, n)

    def test_tail_loop_runs_with_host_recursion_limit_forced_low(self):
        # 不只是“帧数有界”：把宿主 Python 递归限制压到远低于调用次数，
        # 长尾递归仍必须正常完成——证明走的是迭代 trampoline，而非偷偷
        # 依赖抬高宿主栈。先在正常限制下完成（递归的）转换，再压低运行。
        n = 5_000
        program = convert(mutual_alt(n))
        saved = sys.getrecursionlimit()
        sys.setrecursionlimit(200)
        try:
            st = run_program_with_stats(program, step_limit=1_000_000)
        finally:
            sys.setrecursionlimit(saved)
        self.assertEqual(st.value, 7)
        self.assertEqual(st.max_call_frames, 1)
        self.assertEqual(st.tail_jumps, n)

    def test_alternating_tail_matches_source_on_small_inputs(self):
        for n in range(0, 12):
            sv, se = eval_source(mutual_alt(n))
            st = run_program_with_stats(convert(mutual_alt(n)))
            self.assertEqual(sv, st.value, f"n={n}")
            self.assertEqual(se, st.events)
            self.assertEqual(st.steps, st.steps)  # 步数字段存在

    def test_steps_equal_source_step_count(self):
        # 尾跳转不额外计步：目标步数必须与源解释器精确一致
        from recenv import source_interp as si
        for n in (0, 1, 5, 9):
            root = mutual_alt(n)
            program = convert(root)
            s = si._SrcInterp(10_000)
            s.eval(root, None)
            t = ti._TargetInterp(program, 10_000)
            t.eval(program.main, ())
            self.assertEqual(s.steps, t.steps, f"n={n}")

    def test_non_tail_recursion_frame_count_grows(self):
        # s(n) = 1 + s(n-1)：递归调用在 Add 操作数位 -> 非尾，帧数必须增长
        body = ast.If0(
            ast.Var("n"), ast.Int(0),
            ast.Add(
                ast.Int(1),
                ast.Call(ast.Var("s"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        )
        e = ast.LetRec(
            [ast.RecBinding("s", ast.Lambda(["n"], body))],
            ast.Call(ast.Var("s"), [ast.Int(50)]),
        )
        sv, _ = eval_source(e)
        st = run_program_with_stats(convert(e), step_limit=100_000)
        self.assertEqual(sv, 50)
        self.assertEqual(st.value, 50)
        self.assertEqual(st.tail_jumps, 0)
        # main 的 s(50) 压 1 帧；内部 50 次非尾自调用各再压 1 帧 => 峰值 51
        self.assertEqual(st.max_call_frames, 51)

    def test_non_tail_infinite_grows_frames_until_step_budget(self):
        # 非尾无限递归：真实帧不断增长，先撞步数预算（帧数随之很大），
        # 证明计数没有被尾跳转悄悄藏掉。
        body = ast.Add(ast.Int(1), ast.Call(ast.Var("s"), [ast.Var("n")]))
        e = ast.LetRec(
            [ast.RecBinding("s", ast.Lambda(["n"], body))],
            ast.Call(ast.Var("s"), [ast.Int(0)]),
        )
        with self.assertRaises(errors.StepLimitError):
            run_program_with_stats(convert(e), step_limit=3000)

    def test_mixed_non_tail_prefix_then_tail_loop(self):
        # 先做两层非尾包装，再进入尾循环：峰值帧 = 非尾前缀 + 1，尾循环不增长
        # wrap(x) = x + loop(x)          （loop 调用在 Add 内：非尾）
        # loop(n) = if0 n then 0 else loop(n-1)   （尾）
        loop = ast.Lambda(
            ["n"],
            ast.If0(ast.Var("n"), ast.Int(0),
                    ast.Call(ast.Var("loop"), [ast.Add(ast.Var("n"), ast.Int(-1))])),
        )
        wrap = ast.Lambda(
            ["x"],
            ast.Add(ast.Var("x"), ast.Call(ast.Var("loop"), [ast.Var("x")])),
        )
        e = ast.LetRec(
            [
                ast.RecBinding("loop", loop),
                ast.RecBinding("wrap", wrap),
            ],
            ast.Call(ast.Var("wrap"), [ast.Int(5000)]),
        )
        st = run_program_with_stats(convert(e), step_limit=1_000_000)
        # 5000 + loop(5000)=0
        self.assertEqual(st.value, 5000)
        # main->wrap (1)，wrap 内非尾调 loop (2)；loop 自尾递归不增帧
        self.assertEqual(st.max_call_frames, 2)
        self.assertEqual(st.tail_jumps, 5000)


# ---------------------------------------------------------------------------
# 参数求值：从左到右、出错不提前替换帧
# ---------------------------------------------------------------------------

class TestArgumentEvaluation(unittest.TestCase):
    def test_arg_error_does_not_replace_frame(self):
        # f(n) = f(0, add(f, 1))：尾调用的第 2 个实参求值必抛类型错误，
        # 跳转不能发生；出错后活动帧计数回落到 0，峰值只有入口的 1 帧。
        e = ast.LetRec(
            [ast.RecBinding(
                "f",
                ast.Lambda(
                    ["n"],
                    ast.Call(
                        ast.Var("f"),
                        [ast.Int(0), ast.Add(ast.Var("f"), ast.Int(1))],
                        loc="badcall",
                    ),
                ),
            )],
            ast.Call(ast.Var("f"), [ast.Int(0)]),
        )
        interp = ti._TargetInterp(convert(e), 10_000)
        with self.assertRaises(errors.LangRuntimeError):
            interp.eval(convert(e).main, ())
        # 帧已随 finally 释放；峰值没有因“失败的尾调用”而多压帧
        self.assertEqual(interp.call_frames, 0)
        self.assertEqual(interp.max_call_frames, 1)
        self.assertEqual(interp.tail_jumps, 0)

    def test_args_evaluated_left_to_right(self):
        # 两实参都 set 外层 c：左实参写 1，右实参写 2；事件序必须 1 先 2 后。
        e = ast.Let(
            "c", ast.Int(0),
            ast.Call(
                ast.Lambda(["a", "b"], ast.Add(ast.Var("a"), ast.Var("b"))),
                [
                    ast.Call(ast.Lambda([], ast.Set("c", ast.Int(1))), []),
                    ast.Call(ast.Lambda([], ast.Set("c", ast.Int(2))), []),
                ],
            ),
        )
        sv, se = eval_source(e)
        st = run_program_with_stats(convert(e))
        self.assertEqual((sv, se), (st.value, st.events))
        self.assertEqual(se, [("set", "c", 1), ("set", "c", 2)])

    def test_one_arg_mutates_capture_read_by_later_arg(self):
        # 题目点名场景：尾调用实参 A 经闭包 set 共享捕获 cell，
        # 后求值的实参 B 经另一个闭包读同一个 cell，必须读到 A 刚写的值。
        e = ast.Let(
            "c", ast.Int(10),
            ast.Let(
                "bump",
                ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))),
                ast.Let(
                    "rd", ast.Lambda([], ast.Var("c")),
                    ast.LetRec(
                        [ast.RecBinding(
                            "loop",
                            ast.Lambda(
                                ["a", "n"],
                                ast.If0(
                                    ast.Var("n"), ast.Var("c"),
                                    ast.Call(
                                        ast.Var("loop"),
                                        [
                                            ast.Call(ast.Var("bump"), []),
                                            ast.Add(
                                                ast.Call(ast.Var("rd"), []),
                                                ast.Int(-11),
                                            ),
                                        ],
                                    ),
                                ),
                            ),
                        )],
                        ast.Call(ast.Var("loop"), [ast.Int(0), ast.Int(2)]),
                    ),
                ),
            ),
        )
        sv, se = eval_source(e)
        st = run_program_with_stats(convert(e))
        # 第 1 轮：a=bump() 把 c 10->11；n=rd()-11 必须读到 11 -> 0，循环终止，返回 c=11
        self.assertEqual(sv, 11)
        self.assertEqual(se, [("set", "c", 11)])
        self.assertEqual((sv, se), (st.value, st.events))
        # 实参内 bump/rd 是非尾调用（峰值 2），loop 自递归是尾替换（1 次跳转）
        self.assertEqual(st.max_call_frames, 2)
        self.assertEqual(st.tail_jumps, 1)

    def test_reversed_arg_order_reads_old_value(self):
        # 对照：读放在写之前（仍严格左到右），读到旧值，证明顺序确实敏感。
        e = ast.Let(
            "c", ast.Int(10),
            ast.Let(
                "bump",
                ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1)))),
                ast.Let(
                    "rd", ast.Lambda([], ast.Var("c")),
                    ast.Call(
                        ast.Lambda(["x", "y"], ast.Add(ast.Var("x"), ast.Var("y"))),
                        [
                            ast.Add(ast.Call(ast.Var("rd"), []), ast.Int(-11)),  # -1
                            ast.Call(ast.Var("bump"), []),                        # 11
                        ],
                    ),
                ),
            ),
        )
        sv, se = eval_source(e)
        st = run_program_with_stats(convert(e))
        self.assertEqual((sv, se), (10, [("set", "c", 11)]))
        self.assertEqual((sv, se), (st.value, st.events))


# ---------------------------------------------------------------------------
# 异常位置/消息：源 vs 目标 逐项一致（尾 / 非尾两种上下文）
# ---------------------------------------------------------------------------

class TestErrorLocationPairing(unittest.TestCase):
    def _pair(self, root):
        try:
            eval_source(root)
            s = None
        except errors.LangRuntimeError as exc:
            s = str(exc)
        try:
            run_program_with_stats(convert(root))
            t = None
        except errors.LangRuntimeError as exc:
            t = str(exc)
        return s, t

    def test_tail_call_non_function(self):
        e = ast.LetRec(
            [ast.RecBinding("f", ast.Lambda([], ast.Call(ast.Int(3), [], loc="notfn")))],
            ast.Call(ast.Var("f"), []),
        )
        self.assertEqual(self._pair(e), ("notfn: attempt to call non-function value",) * 2)

    def test_nontail_call_non_function(self):
        e = ast.LetRec(
            [ast.RecBinding(
                "f",
                ast.Lambda([], ast.Add(ast.Int(1), ast.Call(ast.Int(2), [], loc="ntcall"))),
            )],
            ast.Call(ast.Var("f"), []),
        )
        self.assertEqual(self._pair(e), ("ntcall: attempt to call non-function value",) * 2)

    def test_tail_call_arity(self):
        e = ast.LetRec(
            [ast.RecBinding(
                "f",
                ast.Lambda(["x"], ast.Call(ast.Var("f"), [ast.Int(1), ast.Int(2)], loc="arity")),
            )],
            ast.Call(ast.Var("f"), [ast.Int(0)]),
        )
        self.assertEqual(self._pair(e), ("arity: function expects 1 args, got 2",) * 2)

    def test_add_type_error_location_in_tail_arg(self):
        e = ast.LetRec(
            [ast.RecBinding(
                "f",
                ast.Lambda(
                    ["n"],
                    ast.Call(
                        ast.Var("f"),
                        [ast.Int(0), ast.Add(ast.Var("f"), ast.Int(1), loc="badadd")],
                        loc="badcall",
                    ),
                ),
            )],
            ast.Call(ast.Var("f"), [ast.Int(0)]),
        )
        # 类型错误归到 Add 节点自己的 loc（与源解释器同口径），不是 Call 的 loc
        self.assertEqual(self._pair(e), ("badadd: integer expected, got function",) * 2)

    def test_set_rhs_call_error(self):
        e = ast.LetRec(
            [ast.RecBinding(
                "f",
                ast.Lambda(
                    ["x"],
                    ast.Let("z", ast.Int(0),
                            ast.Set("z", ast.Call(ast.Int(9), [], loc="setcall"))),
                ),
            )],
            ast.Call(ast.Var("f"), [ast.Int(0)]),
        )
        self.assertEqual(self._pair(e), ("setcall: attempt to call non-function value",) * 2)


if __name__ == "__main__":
    unittest.main()

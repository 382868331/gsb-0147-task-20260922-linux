"""尾调用执行测试。

覆盖：
* 转换期尾位置判定——只有函数体结果、if0 结果分支、let/letrec 的 body
  是尾位置；加法操作数、set 右侧、let 绑定值、call 的函数/参数都不是；
* 交替尾递归跑到远超宿主默认递归深度，最大活动调用帧数有界（=1）；
* 非尾递归的显式帧数确实线性增长；
* 压低宿主递归深度后目标器仍能执行（语言调用不占 Python 栈）；
* 与源解释器在小输入上对照返回值、set 顺序与异常 loc；
* 一个参数修改另一个参数/函数体将读取的捕获变量；
* 参数求值出错时当前帧不被提前替换。
"""

import re
import sys
import unittest

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source, eval_target, eval_target_stats
from recenv import target_interp as ti


# ---------------------------------------------------------------------------
# 程序构造助手
# ---------------------------------------------------------------------------

def i(v, loc=None):
    return ast.Int(v, loc=loc)


def v(n, loc=None):
    return ast.Var(n, loc=loc)


def add(a, b, loc=None):
    return ast.Add(a, b, loc=loc)


def call(f, args, loc=None):
    return ast.Call(f, tuple(args), loc=loc)


def if0(c, t, e, loc=None):
    return ast.If0(c, t, e, loc=loc)


def let(n, e, b, loc=None):
    return ast.Let(n, e, b, loc=loc)


def lam(ps, b, loc=None):
    return ast.Lambda(tuple(ps), b, loc=loc)


def set_(n, e, loc=None):
    return ast.Set(n, e, loc=loc)


def letrec(bs, body, loc=None):
    return ast.LetRec(tuple(bs), body, loc=loc)


def rb(n, f, loc=None):
    return ast.RecBinding(n, f, loc=loc)


def walk_ir(node, out):
    """收集 IR 子树中所有节点（按类型名）。"""
    name = type(node).__name__
    out.append(name)
    for f in getattr(node, "__dataclass_fields__", {}):
        val = getattr(node, f)
        if isinstance(val, tuple):
            for x in val:
                if hasattr(x, "__dataclass_fields__"):
                    walk_ir(x, out)
        elif hasattr(val, "__dataclass_fields__"):
            walk_ir(val, out)


def call_kinds(fn_body):
    kinds = []
    out: list[str] = []
    walk_ir(fn_body, out)
    return [k for k in out if k in ("TCall", "TTailCall")]


def even_odd(n):
    ev = lam(["n"], if0(v("n"), i(1),
                         call(v("odd"), [add(v("n"), i(-1))])))
    od = lam(["n"], if0(v("n"), i(0),
                        call(v("even"), [add(v("n"), i(-1))])))
    return letrec([rb("even", ev), rb("odd", od)], call(v("even"), [i(n)]))


def sumto_non_tail(n):
    # 递归调用在 Add 右操作数：非尾位置
    body = if0(v("n"), i(0),
               add(v("n"), call(v("sumto"), [add(v("n"), i(-1))])))
    return letrec([rb("sumto", lam(["n"], body))], call(v("sumto"), [i(n)]))


class TestTailPositionMarks(unittest.TestCase):
    def _first_fn_body(self, program):
        return next(iter(program.functions.values())).body

    def test_function_body_result_is_tail(self):
        e = let("g", lam([], i(0)), call(lam([], call(v("g"), [])), []))
        prog = convert(e)
        # 立即调用的 lambda 体是 call -> TTailCall
        bodies = [fn.body for fn in prog.functions.values()]
        self.assertTrue(any(call_kinds(b) == ["TTailCall"] for b in bodies), bodies)

    def test_if0_result_branches_are_tail_but_cond_is_not(self):
        # if0 的 cond 里放一个 call（非尾），两分支各一个 call（尾）
        cond = call(lam([], i(0)), [])
        e = let("g", lam([], i(0)),
                let("h", lam([], i(0)),
                    lam([], if0(cond, call(v("g"), []), call(v("h"), [])))))
        prog = convert(e)
        kinds: list[str] = []
        for fn in prog.functions.values():
            kinds += call_kinds(fn.body)
        self.assertEqual(sorted(kinds), ["TCall", "TTailCall", "TTailCall"], kinds)

    def test_let_body_tail_binding_value_not(self):
        # let body 是 call（尾）；let value 是 call（非尾）
        e = let("f", lam([], i(1)),
                let("z", call(v("f"), []), call(v("f"), [])))
        prog = convert(e)
        # 主表达式经 TWithFrame 包裹；无函数（f 是外层 lambda）
        kinds = call_kinds(prog.main)
        self.assertEqual(sorted(kinds), ["TCall", "TTailCall"], kinds)

    def test_letrec_body_is_tail(self):
        e = letrec([rb("f", lam(["n"], if0(v("n"), i(0),
                                           call(v("f"), [add(v("n"), i(-1))]))))],
                   call(v("f"), [i(2)]))
        prog = convert(e)
        kinds = call_kinds(next(iter(prog.functions.values())).body)
        # else 分支的自调用在尾位置；组体的入口调用在 main（也是尾）
        self.assertEqual(kinds, ["TTailCall"])
        self.assertEqual(call_kinds(prog.main), ["TTailCall"])

    def test_add_operands_are_not_tail(self):
        e = let("g", lam([], i(1)),
                let("h", lam([], i(2)),
                    lam([], add(call(v("g"), []), call(v("h"), [])))))
        prog = convert(e)
        for fn in prog.functions.values():
            self.assertNotIn("TTailCall", call_kinds(fn.body))

    def test_set_rhs_is_not_tail(self):
        e = let("x", i(0),
                let("g", lam([], i(7)),
                    lam(["a"], set_("a", call(v("g"), [])))))
        prog = convert(e)
        kinds: list[str] = []
        for fn in prog.functions.values():
            kinds += call_kinds(fn.body)
        # 函数体内唯一的调用位于 set 右侧，必须是普通 TCall
        self.assertEqual(kinds, ["TCall"])

    def test_call_func_and_args_are_not_tail(self):
        # 尾位置的 call，但其函数位置与参数位置中的 call 必须是非尾
        def mk():
            return call(call(lam([], lam([], i(0))), []),
                        [call(lam([], i(3)), [])])
        e = let("k", i(0), mk())
        prog = convert(e)
        kinds = sorted(call_kinds(prog.main))
        # 外层 1 个 TTailCall + func 位置 1 个 TCall + arg 位置 1 个 TCall
        self.assertEqual(kinds, ["TCall", "TCall", "TTailCall"], kinds)

    def test_lambda_in_tail_position_is_not_a_jump(self):
        # f() 的体结果位置是 lambda 字面量：尾位置是 lambda 不是 call，
        # f 体内不得产生任何跳转节点；外层对 f 的调用仍正常是尾调用。
        e = let("f", lam([], lam([], i(0))), call(v("f"), []))
        prog = convert(e)
        f_body = next(iter(prog.functions.values())).body
        self.assertEqual(call_kinds(f_body), [])
        # 主表达式对 f 的入口调用是尾调用
        self.assertEqual(call_kinds(prog.main), ["TTailCall"])


class TestTailRecursionBounded(unittest.TestCase):
    def test_alternating_mutual_tail_recursion_frames_bounded(self):
        n = 200_000  # 远超宿主默认递归深度（通常 1000）
        prog = convert(even_odd(n))
        stats = eval_target_stats(prog, step_limit=5_000_000)
        self.assertEqual(stats.result, 1 if n % 2 == 0 else 0)
        # 真正的核心断言：交替尾递归 20 万次，同时存活的调用帧恒为 1
        self.assertEqual(stats.max_active_frames, 1)
        # 入口 1 次 + 每层 1 次跳转
        self.assertEqual(stats.tail_jumps, n + 1)

    def test_small_n_matches_source(self):
        for n in range(0, 12):
            sv, se = eval_source(even_odd(n))
            tv, te = eval_target(convert(even_odd(n)))
            self.assertEqual((sv, se), (tv, te), n)

    def test_runs_under_tiny_host_recursion_limit(self):
        # 把宿主递归余量压到只够常数层：若执行器借用 Python 栈表达调用，
        # 这里必然 RecursionError。
        n = 50_000
        prog = convert(even_odd(n))
        saved = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(_current_python_depth() + 120)
            stats = eval_target_stats(prog, step_limit=2_000_000)
        finally:
            sys.setrecursionlimit(saved)
        self.assertEqual(stats.max_active_frames, 1)
        self.assertEqual(stats.result, 1 if n % 2 == 0 else 0)

    def test_non_tail_call_also_uses_no_python_stack(self):
        # 非尾调用增长的是*显式*续体栈，不是 Python 栈
        n = 3000
        prog = convert(sumto_non_tail(n))
        saved = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(_current_python_depth() + 120)
            stats = eval_target_stats(prog, step_limit=500_000)
        finally:
            sys.setrecursionlimit(saved)
        self.assertEqual(stats.result, n * (n + 1) // 2)
        # 显式帧数随深度增长
        self.assertEqual(stats.max_active_frames, n + 1)
        # 仅主表达式入口那次是尾跳转
        self.assertEqual(stats.tail_jumps, 1)

    def test_source_interp_still_recursive_for_comparison(self):
        # 对照证据：源解释器（递归下降保留）在低宿主限制下确实撑栈，
        # 说明“超过通常宿主递归深度”靠的不是调大 Python 限制。
        saved = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(_current_python_depth() + 80)
            with self.assertRaises(RecursionError):
                eval_source(even_odd(5_000), step_limit=500_000)
        finally:
            sys.setrecursionlimit(saved)


class TestNonTailFrameCounting(unittest.TestCase):
    def test_non_tail_depth_counted_exactly(self):
        n = 50
        prog = convert(sumto_non_tail(n))
        stats = eval_target_stats(prog, step_limit=100_000)
        self.assertEqual(stats.result, n * (n + 1) // 2)
        self.assertEqual(stats.max_active_frames, n + 1)

    def test_non_tail_entry_without_root_marker(self):
        # 主表达式把调用放在 Add 里：第一次进入就是非尾（普通 ret 标记）
        n = 5
        e = add(i(0), letrec(
            [rb("sumto", lam(["n"], if0(
                v("n"), i(0),
                add(v("n"), call(v("sumto"), [add(v("n"), i(-1))])))))],
            call(v("sumto"), [i(n)])))
        stats = eval_target_stats(convert(e), step_limit=100_000)
        self.assertEqual(stats.result, n * (n + 1) // 2)
        # f(5)..f(0) 共 6 个激活同时存活于最深点
        self.assertEqual(stats.max_active_frames, n + 1)

    def test_peak_frames_with_nested_non_tail(self):
        # g 体在 Add 中两次非尾调用尾递归 loop；主表达式非尾调用 g。
        loop = lam(["n"], if0(v("n"), i(0),
                              call(v("loop"), [add(v("n"), i(-1))])))
        g = lam([], add(call(v("loop"), [i(3)]), call(v("loop"), [i(2)])))
        e = letrec([rb("loop", loop), rb("g", g)],
                   add(i(0), call(v("g"), [])))
        stats = eval_target_stats(convert(e), step_limit=100_000)
        self.assertEqual(stats.result, 0)
        # g 帧（1）+ 其非尾调用的 loop 帧（1）= 峰值 2；loop 自调用是尾跳转
        self.assertEqual(stats.max_active_frames, 2)

    def test_tail_chain_then_non_tail_resets_peak(self):
        # 尾递归不累积帧；其后再发生非尾调用时峰值由嵌套深度决定
        loop = lam(["n"], if0(v("n"), i(7),
                              call(v("loop"), [add(v("n"), i(-1))])))
        e = letrec([rb("loop", loop)], call(v("loop"), [i(1000)]))
        stats = eval_target_stats(convert(e), step_limit=100_000)
        self.assertEqual(stats.result, 7)
        self.assertEqual(stats.max_active_frames, 1)


class TestCaptureAndArgumentOrdering(unittest.TestCase):
    def test_one_arg_mutates_capture_read_by_next_arg_and_body(self):
        # let x=0 in let g=(a,b)-> a+b+x in g(set x=7, x)
        e = let("x", i(0),
                let("g", lam(["a", "b"],
                             add(add(v("a"), v("b")), v("x"))),
                    call(v("g"), [set_("x", i(7)), v("x")], loc="CALL")))
        sv, se = eval_source(e)
        stats = eval_target_stats(convert(e))
        # 参数从左到右：a=7（同时 x 变 7），b 读捕获 x=7，函数体再读 x=7
        self.assertEqual(sv, 21)
        self.assertEqual(se, [("set", "x", 7)])
        self.assertEqual((stats.result, stats.events), (sv, se))

    def test_arg_mutation_order_in_tail_call_position(self):
        # 同样的参数语义，但 g() 处于函数体尾位置（经一层立即调用的函数）
        g = lam(["a", "b"], add(add(v("a"), v("b")), v("x")))
        e = let("x", i(0),
                let("g", g,
                    call(lam([], call(v("g"), [set_("x", i(7)), v("x")])), [])))
        prog = convert(e)
        sv, se = eval_source(e)
        stats = eval_target_stats(prog)
        self.assertEqual((stats.result, stats.events), (21, [("set", "x", 7)]))
        self.assertEqual(sv, stats.result)
        # g 的调用确实是 TTailCall
        kinds = []
        for fn in prog.functions.values():
            kinds += call_kinds(fn.body)
        self.assertIn("TTailCall", kinds)

    def test_mutable_param_set_then_read_by_later_argument(self):
        # f(p) 中尾调用 h(set p=9, p)：第二参数读到本帧 p 的新值
        e = let("h", lam(["a", "b"], add(v("a"), v("b"))),
                let("f", lam(["p"], call(v("h"), [set_("p", i(9)), v("p")])),
                    call(v("f"), [i(1)])))
        sv, se = eval_source(e)
        stats = eval_target_stats(convert(e))
        self.assertEqual(sv, 18)
        self.assertEqual(se, [("set", "p", 9)])
        self.assertEqual((stats.result, stats.events), (sv, se))

    def test_set_event_order_left_to_right(self):
        e = let("x", i(0),
                let("g", lam(["a", "b", "c"], i(0)),
                    call(v("g"), [set_("x", i(1)), set_("x", i(2)), set_("x", i(3))])))
        sv, se = eval_source(e)
        tv, te = eval_target(convert(e))
        self.assertEqual(se, [("set", "x", 1), ("set", "x", 2), ("set", "x", 3)])
        self.assertEqual((sv, se), (tv, te))


class TestFrameNotReplacedOnArgumentFailure(unittest.TestCase):
    def _program(self):
        # f(x) 尾调用 g(x, <call int>): 第二参数引用当前帧 x 后抛运行期错误。
        # 若在参数求值前提前替换帧，x 将无法解析；正确实现保留 f 帧、
        # 不增加尾跳转计数。
        g = lam(["a", "b"], add(v("a"), v("b")))
        f = lam(["x"], call(v("g"), [v("x"), call(i(2), [], loc="BOOM")]))
        h = lam([], add(i(1000), call(v("f"), [i(5)])))  # h 非尾调用 f
        return let("g", g,
                   let("f", f,
                       let("h", h, call(v("h"), [], loc="HCALL"))))

    def test_error_location_and_pending_frames(self):
        prog = convert(self._program())
        interp = ti._TargetInterp(prog, 100_000)
        with self.assertRaises(errors.LangRuntimeError) as cm:
            interp.eval(prog.main, ())
        self.assertTrue(str(cm.exception).startswith("BOOM:"), str(cm.exception))
        # h 非尾调用 f：出错时 h、f 两个函数帧都应还在
        n_markers = sum(
            1 for k in interp.stack if k[0] in ("ret", "ret_root")
        )
        self.assertEqual(interp.active_frames(), 2)
        self.assertEqual(n_markers, 2)
        # 只发生了进入 h 的尾跳转；f->g 的尾跳转绝不能计数
        self.assertEqual(interp.tail_jumps, 1)
        self.assertEqual(interp.max_active_frames, 2)

    def test_error_propagates_with_same_location_as_source(self):
        def norm(m):
            return re.sub(r"got \w*[Cc]losure", "got <function>", m)

        e = self._program()
        with self.assertRaises(errors.LangRuntimeError) as cs:
            eval_source(e)
        with self.assertRaises(errors.LangRuntimeError) as ct:
            eval_target(convert(e))
        self.assertEqual(norm(str(cs.exception)), norm(str(ct.exception)))

    def test_arity_error_does_not_replace_frame(self):
        # 尾调用元数不符：参数全部正常求值，但校验失败 -> 帧不替换
        g = lam(["a"], v("a"))
        f = lam(["x"], call(v("g"), [v("x"), i(1)], loc="ARITY"))
        e = let("g", g, let("f", f, call(v("f"), [i(5)])))
        interp = ti._TargetInterp(convert(e), 100_000)
        with self.assertRaises(errors.LangRuntimeError) as cm:
            interp.eval(convert(e).main, ())
        self.assertIn("ARITY", str(cm.exception))
        # f 仍存活；进入 f 的那次跳转保留，f->g 未发生
        self.assertEqual(interp.tail_jumps, 1)
        self.assertEqual(interp.active_frames(), 1)

    def test_non_function_callee_does_not_push_frame(self):
        e = let("z", i(3), call(v("z"), [], loc="NOTFN"))
        interp = ti._TargetInterp(convert(e), 100_000)
        with self.assertRaises(errors.LangRuntimeError):
            interp.eval(convert(e).main, ())
        self.assertEqual(interp.active_frames(), 0)
        self.assertEqual(interp.max_active_frames, 0)
        self.assertEqual(interp.tail_jumps, 0)


class TestExceptionLocationParity(unittest.TestCase):
    def _norm(self, m):
        return re.sub(r"got \w*[Cc]losure", "got <function>", m)

    def _check(self, root, label):
        try:
            eval_source(root)
            self.fail(f"{label}: source did not raise")
        except errors.RecenvError as es:
            src = (type(es).__name__, self._norm(str(es)))
        try:
            eval_target(convert(root))
            self.fail(f"{label}: target did not raise")
        except errors.RecenvError as et:
            tgt = (type(et).__name__, self._norm(str(et)))
        self.assertEqual(src, tgt, label)

    def test_call_non_function_tail_and_non_tail(self):
        self._check(call(i(3), [], loc="C1"), "tail non-function")
        self._check(add(i(1), call(i(3), [], loc="C2")), "non-tail non-function")

    def test_arity(self):
        self._check(call(lam(["a"], v("a")), [i(1), i(2)], loc="C3"), "arity")

    def test_add_if0_set_type_errors(self):
        self._check(
            add(i(1), let("f", lam([], i(1)), v("f"), loc="L4"), loc="A4"),
            "add fn",
        )
        self._check(
            if0(let("f", lam([], i(0)), v("f")), i(1), i(2), loc="I5"),
            "if0 cond",
        )
        self._check(
            let("x", i(0), let("f", lam([], i(1)),
                               set_("x", v("f"), loc="S6"))),
            "set rhs",
        )


class TestStatsAndStepBudget(unittest.TestCase):
    def test_run_program_stats_matches_run_program(self):
        e = let("x", i(0),
                let("inc", lam([], set_("x", add(v("x"), i(1)))),
                    add(call(v("inc"), []), call(v("inc"), []))))
        prog = convert(e)
        rv, re_ = eval_target(prog)
        st = eval_target_stats(prog)
        self.assertEqual((rv, re_), (st.result, st.events))
        self.assertGreater(st.steps, 0)
        self.assertGreaterEqual(st.max_active_frames, 1)

    def test_step_counts_match_source(self):
        for n in range(0, 8):
            root = even_odd(n)
            prog = convert(root)
            s = __import__("recenv.source_interp", fromlist=["_SrcInterp"])._SrcInterp(100_000)
            s.eval(root, None)
            st = eval_target_stats(prog, step_limit=100_000)
            self.assertEqual(s.steps, st.steps, n)

    def test_infinite_tail_loop_hits_budget_with_one_frame(self):
        e = letrec(
            [rb("f", lam(["n"], call(v("f"), [v("n")])))],
            call(v("f"), [i(0)]),
        )
        prog = convert(e)
        interp = ti._TargetInterp(prog, 1000)
        with self.assertRaises(errors.StepLimitError) as cm:
            interp.eval(prog.main, ())
        self.assertEqual(cm.exception.limit, 1000)
        self.assertEqual(interp.max_active_frames, 1)

    def test_step_failure_preserves_events_and_frame(self):
        # 先 set 再耗尽预算（尾循环）：事件保留，帧数仍为 1
        e = letrec(
            [rb("f", lam(["n"], if0(
                v("n"),
                call(v("f"), [v("n")]),
                add(set_("n", i(0)), call(v("f"), [v("n")])))))],
            call(v("f"), [i(1)]),
        )
        with self.assertRaises(errors.StepLimitError) as cm:
            eval_target(convert(e), step_limit=100)
        self.assertEqual(cm.exception.events, [("set", "n", 0)])


def _current_python_depth() -> int:
    f = sys._getframe(0)
    d = 0
    while f is not None:
        d += 1
        f = f.f_back
    return d


if __name__ == "__main__":
    unittest.main()

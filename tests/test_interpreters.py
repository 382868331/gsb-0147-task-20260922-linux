"""源参考解释器与目标解释器的独立对照测试。

期望值由手写常量或完全独立的 ``source_ref``（不 import scope/convert/
target）直接运行源 AST 得到；往返一致只作为补充断言。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from closure_layout import parse, convert, run, run_source  # noqa: E402
from closure_layout.errors import (  # noqa: E402
    LangTypeError, ArityError, StepLimitError,
    UnboundVariableError, UninitializedSlotError,
)
from closure_layout.target import NONINT_TRACE  # noqa: E402
from _proggen import generate  # noqa: E402


# ---------------------------------------------------------- 手写期望常量

HAND_CASES = [
    # (源码, 期望返回值)
    ("(+ 2 3)", 5),
    ("(if0 0 7 8)", 7),
    ("(if0 9 7 8)", 8),
    ("((lambda (x) (+ x 40)) 2)", 42),
    ("(let (x 10) ((lambda (y) (+ x y)) 5))", 15),
    # 严格从左到右求值（set 的求值顺序影响结果）。
    ("(let (x 1) (+ (set x 5) x))", 10),
    ("(let (x 1) (+ x (set x 5)))", 6),
    # 同名遮蔽：三个 x 互不干扰。
    ("(let (x 1) (let (x (+ x 10)) (+ x (let (x 100) x))))", 111),
    # 闭包间共享可变 cell 的计数行为（set 返回新值：1+2，再读 c=2）。
    ("(let (c 0) (let (inc (lambda () (set c (+ c 1)))) "
     "(+ (+ (inc) (inc)) ((lambda () c)))))", 5),
    # 自递归（n + (n-1) + ... + 1，基准 1：5+4+3+2+1+1=16）。
    ("(letrec ((fact (lambda (n) (if0 n 1 (+ n (fact (+ n -1))))))) "
     "(fact 5))", 16),
    # 互递归：偶数/奇数。
    ("(letrec ((e (lambda (n) (if0 n 1 (o (+ n -1))))) "
     "        (o (lambda (n) (if0 n 0 (e (+ n -1)))))) (e 10))", 1),
    ("(letrec ((e (lambda (n) (if0 n 1 (o (+ n -1))))) "
     "        (o (lambda (n) (if0 n 0 (e (+ n -1)))))) (e 11))", 0),
    # letrec 成员捕获外层自由变量（a 不递减，b 每次 -1，必然到 0）。
    ("(let (base 100) "
     "(letrec ((a (lambda (n) (if0 n base (b n)))) "
     "        (b (lambda (n) (a (+ n -1))))) (a 3)))", 100),
    # 组函数被重新绑定（组帧槽可写）。
    ("(letrec ((f (lambda (n) (if0 n 0 (+ 1 (f (+ n -1))))))) "
     "(let (g (set f (lambda (n) 9))) (f 7)))", 9),
]


class TestHandComputed(unittest.TestCase):
    def test_hand_cases_source(self):
        for src, expected in HAND_CASES:
            with self.subTest(src=src):
                self.assertEqual(run_source(parse(src)).value, expected)

    def test_hand_cases_target(self):
        for src, expected in HAND_CASES:
            with self.subTest(src=src):
                self.assertEqual(run(convert(parse(src))).value, expected)


class TestSideEffectTraces(unittest.TestCase):
    def test_trace_sequence_order(self):
        src = ("(let (x 0) "
               "(+ (set x 1) (+ (set x 2) (set x 3))))")
        s = run_source(parse(src))
        t = run(convert(parse(src)))
        # 同一 set 编号规则下，两侧轨迹一致且按左到右发生。
        self.assertEqual(s.trace, [(0, 1), (1, 2), (2, 3)])
        self.assertEqual(t.trace, s.trace)

    def test_trace_shared_cell_observation(self):
        # inc 两次后读取：副作用序列与最终值两侧一致。
        src = ("(let (c 0) (let (inc (lambda () (set c (+ c 1)))) "
               "(let (get (lambda () c)) (+ (+ (inc) (inc)) (get)))))")
        s = run_source(parse(src))
        t = run(convert(parse(src)))
        self.assertEqual(s.value, 5)
        self.assertEqual(t.value, 5)
        self.assertEqual(s.trace, t.trace)
        self.assertEqual([v for _, v in t.trace], [1, 2])

    def test_trace_non_integer_assignment_marker(self):
        # 把闭包赋给 x，随后调用：轨迹记录非整数标记，返回值为整数。
        src = "(let (x 1) (let (g (set x (lambda () 5))) (x)))"
        s = run_source(parse(src))
        t = run(convert(parse(src)))
        self.assertEqual(s.value, 5)
        self.assertEqual(t.value, 5)
        self.assertEqual(s.trace, t.trace)
        self.assertEqual(t.trace[0][1], NONINT_TRACE)


class TestClosureSharingCount(unittest.TestCase):
    """闭包共享计数：通过可观察行为验证两个闭包共享同一 cell。"""

    def test_two_mutators_share_state(self):
        src = ("(let (c 0) "
               "(let (a (lambda () (set c (+ c 1)))) "
               "(let (b (lambda () (set c (+ c 10)))) "
               "(+ (+ (a) (b)) ((lambda () c))))))")
        s = run_source(parse(src))
        t = run(convert(parse(src)))
        # (a): c=1 返回 1；(b): c=11 返回 11；读 c=11；合计 1+11+11=23。
        self.assertEqual(s.value, 23)
        self.assertEqual(t.value, 23)
        self.assertEqual(t.trace, s.trace)
        self.assertEqual([v for _, v in t.trace], [1, 11])

    def test_cell_not_shared_across_distinct_bindings(self):
        # 两次调用工厂应各自独立计数（每次调用新建 let 槽）。
        src = ("(let (mk (lambda (init) "
        "        (let (c init) (lambda () (set c (+ c 1)))))) "
        "(let (p (mk 0)) (let (q (mk 0)) (+ (p) (q)))))")
        s = run_source(parse(src))
        t = run(convert(parse(src)))
        self.assertEqual(s.value, 2)
        self.assertEqual(t.value, 2)


class TestMutualRecursion(unittest.TestCase):
    def test_three_function_cycle(self):
        src = ("(let (base 100) "
               "(letrec ((a (lambda (n) (if0 n base (b n)))) "
               "        (b (lambda (n) (c (+ n -1)))) "
               "        (c (lambda (n) (a (+ n -1))))) "
               "(a 4)))")
        t = run(convert(parse(src)))
        self.assertEqual(t.value, run_source(parse(src)).value)

    def test_nested_lambda_inside_member(self):
        # 成员函数体内再建闭包，捕获外层变量与同组帧。
        src = ("(let (k 2) "
               "(letrec ((a (lambda (n) ((lambda () (+ k (b n))))) ) "
               "        (b (lambda (n) (+ 1 n)))) (a 40)))")
        t = run(convert(parse(src)))
        self.assertEqual(t.value, 43)

    def test_group_frame_escapes_and_member_called(self):
        # 组帧随一个捕获成员函数的闭包逃逸到组外再调用。
        src = ("(let (g (letrec ((a (lambda () 7))) "
               "         (lambda () (a)))) (g))")
        tree = parse(src)
        self.assertEqual(run(convert(tree)).value, 7)
        self.assertEqual(run_source(tree).value, 7)

    def test_group_frame_escapes_as_argument(self):
        src = ("((lambda (thunk) (thunk)) "
               "(letrec ((a (lambda () 9))) (lambda () (a))))")
        tree = parse(src)
        self.assertEqual(run(convert(tree)).value,
                         run_source(tree).value)

    def test_group_function_rebound_via_mutator(self):
        # 组函数槽经组帧被外部 mutator 重绑后，调用取到新闭包。
        src = ("(letrec ((f (lambda () 1))) "
               "(let (h (lambda () (set f (lambda () 5)))) "
               "(let (z (h)) (+ 0 (f)))))")
        tree = parse(src)
        s, t = run_source(tree), run(convert(tree))
        self.assertEqual((s.value, s.trace), (t.value, t.trace))
        self.assertEqual(t.value, 5)

    def test_boxed_parameter_end_to_end(self):
        # 形参被内层闭包 set：调用约定传原始值，函数入口装箱。
        src = "((lambda (x) (+ ((lambda () (set x 9))) x)) 1)"
        tree = parse(src)
        self.assertEqual(run_source(tree).value, 18)
        self.assertEqual(run(convert(tree)).value, 18)


class TestDifferentialGenerated(unittest.TestCase):
    """确定性生成程序的批量独立对照（返回值、副作用、错误类别）。"""

    def test_generated_programs_parity(self):
        programs = generate(seed=20260922, count=120, depth=5)
        checked = 0
        for src in programs:
            tree = parse(src)
            sk, sr = _outcome(lambda: run_source(tree))
            tk, tr = _outcome(lambda: run(convert(tree)))
            self.assertEqual(
                sk, tk,
                msg=f"错误类别不一致 src={sk} tgt={tk}\n{src}")
            if sk == "ok":
                checked += 1
                self.assertEqual(sr.value, tr.value, msg=src)
                self.assertEqual(sr.trace, tr.trace, msg=src)
        self.assertGreater(checked, 80)  # 绝大多数应正常结束


def _outcome(fn):
    try:
        return "ok", fn()
    except (LangTypeError, ArityError, StepLimitError,
            UnboundVariableError, UninitializedSlotError) as e:
        return type(e).__name__, None


if __name__ == "__main__":
    unittest.main()

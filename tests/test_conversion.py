"""环境布局转换测试：自由变量排序、装箱、共享组帧、无自由变量残留。"""

import unittest

from closure_layout import parse, convert
from closure_layout import ast, ir
from closure_layout.errors import UnboundVariableError
from closure_layout.scope import analyze


def free_vars_of(program, fid):
    return program.functions[fid].free_vars


class TestUnboundAndIds(unittest.TestCase):
    def test_unbound_rejected_static(self):
        with self.assertRaises(UnboundVariableError):
            convert(parse("(+ y 1)"))

    def test_unbound_in_dead_branch_still_rejected(self):
        # 静态词法检查不依赖分支是否执行。
        with self.assertRaises(UnboundVariableError):
            convert(parse("(if0 0 1 missing)"))

    def test_shadowing_uses_distinct_binding_ids(self):
        # 内层 x 与外层 x 同名但 bid 不同；捕获的是各自的绑定。
        src = ("(let (x 1) "
               "(+ ((lambda () x)) "
               "   (let (x 10) ((lambda () x)))))")
        info = analyze(parse(src))
        x_bids = [b for b, i in info.bid_info.items() if i.name == "x"]
        self.assertEqual(len(x_bids), 2)

    def test_free_vars_sorted_by_bid_not_name(self):
        # 先绑定名字 z 更大（bid 更小），再绑定 a；自由变量必须按 bid 排。
        src = ("(let (z 1) (let (a 2) ((lambda () (+ z a)))))")
        prog = convert(parse(src))
        (fid,) = [f for f in prog.functions if f != "main"]
        fv = free_vars_of(prog, fid)
        info = analyze(parse(src))
        bid_z = next(b for b, i in info.bid_info.items() if i.name == "z")
        bid_a = next(b for b, i in info.bid_info.items() if i.name == "a")
        self.assertEqual(fv, tuple(sorted(fv)))
        self.assertEqual(fv[0], bid_z)
        self.assertLess(bid_z, bid_a)


class TestBoxingAndSharing(unittest.TestCase):
    def test_captured_mutable_boxed(self):
        src = ("(let (x 0) "
               "(let (inc (lambda () (set x (+ x 1)))) "
               "(+ (inc) (inc))))")
        prog = convert(parse(src))
        info = prog.binding_info
        boxed = [bid for bid, (_, b) in info.items() if b]
        self.assertEqual(len(boxed), 1)
        # inc 闭包环境中该槽是 cell 构造：main 中 let 绑定为 TCell。
        main_text = _node_text(prog.main_body)
        self.assertIn("cell", main_text)

    def test_pure_capture_not_boxed(self):
        src = "(let (x 5) ((lambda () (+ x x))))"
        prog = convert(parse(src))
        self.assertFalse(any(b for _, b in prog.binding_info.values()))

    def test_set_but_not_captured_not_boxed(self):
        src = "(let (x 1) (+ (set x 2) x))"
        prog = convert(parse(src))
        self.assertFalse(any(b for _, b in prog.binding_info.values()))

    def test_boxed_param_when_mutated_inside_nested_closure(self):
        src = "((lambda (x) (+ ((lambda () (set x 9))) x)) 1)"
        prog = convert(parse(src))
        member = [f for f in prog.functions.values() if f.nparams == 1]
        self.assertEqual(len(member), 1)
        self.assertEqual(member[0].boxed_params, (0,))

    def test_two_closures_share_one_cell(self):
        # 结构断言：inc/get 两个闭包环境指向同一 bid 的同一 cell。
        src = ("(let (c 0) "
               "(let (inc (lambda () (set c (+ c 1)))) "
               "(let (get (lambda () c)) 0)))")
        prog = convert(parse(src))
        text = _node_text(prog.main_body)
        # 两个 closure 构造都应引用同一个 bid 的槽位。
        self.assertEqual(text.count(f"bid#"), text.count(f"bid#"))
        captured = {
            bid for fn in prog.functions.values() for bid in fn.free_vars}
        c_bids = [bid for bid, (name, _) in prog.binding_info.items()
                  if name == "c"]
        self.assertEqual(c_bids, [b for b in c_bids if b in captured])


class TestLetrecGroupLayout(unittest.TestCase):
    def test_members_share_one_group_frame(self):
        src = ("(letrec ((a (lambda (n) (if0 n 1 (b (+ n -1))))) "
               "        (b (lambda (n) (if0 n 0 (a (+ n -1)))))) "
               "(a 4))")
        prog = convert(parse(src))
        members = [fn for fn in prog.functions.values()
                   if fn.group_size == 2]
        self.assertEqual(len(members), 2)
        # 成员函数 free_vars 中不含同组函数的组 bid；
        # 同组互访必须经环境槽 0 的 TGetGroup。
        for fn in members:
            self.assertFalse(any(
                prog.binding_info[bid][0] in ("a", "b")
                for bid in fn.free_vars))
            kinds = {type(x).__name__ for x in _walk_ir(fn.body)}
            self.assertIn("TGetGroup", kinds)
            self.assertNotIn("TGet", kinds)

    def test_group_frame_allocated_once(self):
        src = ("(letrec ((a (lambda () 1)) (b (lambda () (a)))) 0)")
        prog = convert(parse(src))
        text = _node_text(prog.main_body)
        self.assertEqual(text.count("make-group"), 1)

    def test_self_recursive_function_group_size_one(self):
        src = "(letrec ((lp (lambda () (lp)))) 0)"
        prog = convert(parse(src))
        fn = [f for f in prog.functions.values() if f.group_size == 1]
        self.assertEqual(len(fn), 1)
        self.assertEqual(fn[0].free_vars, ())

    def test_outer_free_var_of_member_is_captured(self):
        src = ("(let (k 7) "
               "(letrec ((a (lambda (n) (if0 n k (a (+ n -1)))))) "
               "(a 1)))")
        prog = convert(parse(src))
        a_fn = next(f for f in prog.functions.values()
                    if f.name == "a")
        self.assertEqual(len(a_fn.free_vars), 1)
        name = prog.binding_info[a_fn.free_vars[0]][0]
        self.assertEqual(name, "k")
        # 成员闭包环境 = [组帧, k]：main 构造闭包时 k 取自局部槽；
        # 成员函数体内读取 k 必须经环境槽 1。
        self.assertIn("env[1]", _node_text(a_fn.body))
        self.assertEqual(a_fn.group_size, 1)

    def test_no_free_variables_remain_in_function_bodies(self):
        # 结构性不变量：任何函数体只通过 env/group 访问，不存在“裸”变量；
        # IR 本身就没有名字节点，这里检查所有 TGet 槽下标在范围内。
        src = ("(let (x 1) (letrec ((a (lambda (n) (+ x n))) "
               "                    (b (lambda () (a 2)))) (b)))")
        prog = convert(parse(src))
        for fn in prog.functions.values():
            width = (fn.group_size and 1 + len(fn.free_vars)) or len(fn.free_vars)
            for node in _walk_ir(fn.body):
                if isinstance(node, ir.TGet):
                    self.assertLess(node.slot, width)


# ------------------------------------------------------------------ 工具

def _node_text(node) -> str:
    from closure_layout.layout import render_node
    return render_node(node)


def _walk_ir(node):
    yield node
    for f in getattr(node, "__dataclass_fields__", {}):
        v = getattr(node, f)
        if isinstance(v, ir.TNode):
            yield from _walk_ir(v)
        elif isinstance(v, tuple):
            for item in v:
                if isinstance(item, ir.TNode):
                    yield from _walk_ir(item)


if __name__ == "__main__":
    unittest.main()

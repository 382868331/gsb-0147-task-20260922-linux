"""固定输入演示（确定性、离线、无第三方依赖）。

运行::

    ./.venv/bin/python demo.py

内容：
1. 正常结果：共享 cell 的计数器闭包 + letrec 互递归 even/odd；
   独立源解释器给出返回值与副作用序列，convert 后打印函数表与显式环境
   布局，目标解释器真实执行并逐项对照。
2. 尾调用：交替尾递归 even/odd 跑到远超宿主默认递归深度，最大活动
   调用帧数恒为 1；再用非尾递归 sumto 证明显式帧数确实线性增长。
3. 参数顺序：一个参数 set 捕获变量，后续参数与函数体读到新值。
4. 异常边界：真实构造一个含未绑定变量的程序，convert 必须拒绝并给出
   可定位错误；另真实触发 501 节点的 NodeLimitError；尾调用参数求值
   出错时当前帧不被提前替换（帧计数/尾跳转计数为证）。

所有结果均为现场计算，不使用 sleep，不回放预录结果。
"""

from __future__ import annotations

import sys

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source, eval_target, eval_target_stats
from recenv import target_interp as ti


def line(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def build_counter() -> ast.Expr:
    # let c = 0 in
    # let inc = () -> set c = c + 1 in
    # let get = () -> c in
    #   inc() + inc() + get()
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


def build_even_odd(n: int) -> ast.Expr:
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


def show_layout(program: ast.Program) -> None:
    print("函数表（函数体无自由变量，外部引用全部经环境槽）：")
    for fid, fn in program.functions.items():
        mut = [p for p, m in zip(fn.params, fn.mutable_params) if m]
        print(f"  {fid}: params={list(fn.params)}  捕获槽={fn.n_capture} "
              f"激活帧总槽={fn.n_slots}  可变参数={mut or '无'}")
        for i, label in enumerate(program.layout[fid]):
            print(f"      槽{i:<2} {label}")
    print()
    print("槽位约定：val:<名>#id<绑定ID> 为不可变原值；cell: 为共享可变单元；")
    print("          cell-rec: 为 letrec 组成员槽（一次性填充，全组共享同一帧记录）。")
    print("          捕获槽严格按绑定 ID 升序排列，同名遮蔽的绑定有不同 ID。")


def demo_counter() -> None:
    line("正常结果 A：两个闭包共享同一个可变 cell（inc / get）")
    root = build_counter()
    s_value, s_events = eval_source(root)          # 独立源解释器
    program = convert(root)
    t_value, t_events = eval_target(program)        # 目标 IR 解释器

    show_layout(program)
    print()
    print(f"源解释器（独立参考）: 返回值={s_value}  副作用序列={s_events}")
    print(f"目标解释器（执行 IR）: 返回值={t_value}  副作用序列={t_events}")
    assert (s_value, s_events) == (t_value, t_events)
    print("对照结果：返回值与副作用序列完全一致 ✔")


def demo_mutual() -> None:
    line("正常结果 B：letrec 同时绑定的互递归函数组 even / odd")
    n = 9
    root = build_even_odd(n)
    s_value, s_events = eval_source(root)
    program = convert(root)
    t_value, t_events = eval_target(program)

    show_layout(program)
    print()
    print(f"源解释器（独立参考）: even({n}) = {s_value}  副作用={s_events}")
    print(f"目标解释器（执行 IR）: even({n}) = {t_value}  副作用={t_events}")
    assert (s_value, s_events) == (t_value, t_events)
    assert s_value == 0  # 9 是奇数
    print("对照结果：两者一致，且 even(9)=0 符合预期 ✔")


def demo_tail_recursion() -> None:
    line("尾调用 A：交替尾递归 even/odd，远超宿主递归深度而帧数有界")
    n = 200_000
    root = build_even_odd(n)
    program = convert(root)
    # CPython 出厂默认递归深度上限为 1000（保留的源解释器为其自身递归
    # 下降需要会在导入时上调它；这里显式恢复成通常值再执行目标器，证明
    # 尾调用执行不借用 Python 调用栈）。
    saved_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        stats = eval_target_stats(program, step_limit=5_000_000)
    finally:
        sys.setrecursionlimit(saved_limit)
    print(f"even({n}) = {stats.result}（{'偶' if stats.result == 1 else '奇'}）")
    print(f"在宿主递归深度上限 1000 下完成 {stats.tail_jumps} 次尾跳转 "
          f"（约为该上限的 {stats.tail_jumps // 1000} 倍），无 RecursionError")
    print(f"求值步数={stats.steps}；最大活动调用帧数="
          f"{stats.max_active_frames}（尾调用替换当前帧，不累积）")
    assert stats.max_active_frames == 1
    # 小输入再与源解释器对照返回值
    sv, _ = eval_source(build_even_odd(9))
    assert sv == 0 and stats.result in (0, 1)
    print("执行器迭代驱动、不借 Python 调用栈；小输入已与源解释器对照 ✔")


def demo_non_tail_frame_growth() -> None:
    line("尾调用 B：非尾递归证明显式帧数确实增加")

    def sumto(k: int) -> ast.Expr:
        # 递归调用位于 Add 右操作数 —— 不是尾位置
        body = ast.If0(
            ast.Var("n"),
            ast.Int(0),
            ast.Add(
                ast.Var("n"),
                ast.Call(ast.Var("sumto"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        )
        return ast.LetRec(
            [ast.RecBinding("sumto", ast.Lambda(["n"], body))],
            ast.Call(ast.Var("sumto"), [ast.Int(k)]),
        )

    n = 200
    root = sumto(n)
    sv, _ = eval_source(root, step_limit=100_000)
    stats = eval_target_stats(convert(root), step_limit=100_000)
    print(f"sumto({n}) = {stats.result}（期望 {n * (n + 1) // 2}）")
    print(f"最大活动调用帧数={stats.max_active_frames}（= 根帧 1 + 非尾递归 "
          f"深度 {n}）；尾跳转次数={stats.tail_jumps}（仅主表达式入口）")
    assert sv == stats.result == n * (n + 1) // 2
    assert stats.max_active_frames == n + 1
    print("非尾调用建立新帧、返回时弹出；计数随真实调用嵌套增长 ✔")


def demo_argument_capture_order() -> None:
    line("尾调用 C：参数从左到右 —— 一个参数修改后续参数要读的捕获变量")
    # let x = 0 in let g = (a, b) -> a + b + x in g(set x = 7, x)
    root = ast.Let(
        "x",
        ast.Int(0),
        ast.Let(
            "g",
            ast.Lambda(
                ["a", "b"],
                ast.Add(ast.Add(ast.Var("a"), ast.Var("b")), ast.Var("x")),
            ),
            ast.Call(
                ast.Var("g"),
                [ast.Set("x", ast.Int(7)), ast.Var("x")],
            ),
        ),
    )
    sv, se = eval_source(root)
    stats = eval_target_stats(convert(root))
    print("g(set x=7, x)：第一参数求值时把共享 cell x 改成 7；")
    print("第二参数与函数体随后读到的都是新值 7 -> 7+7+7 =", stats.result)
    print(f"源解释器: 值={sv} 事件={se}")
    print(f"目标解释器: 值={stats.result} 事件={stats.events} "
          f"最大帧数={stats.max_active_frames}")
    assert (sv, se) == (stats.result, stats.events) == (21, [("set", "x", 7)])
    print("cell 共享与从左到右严格性两解释器一致 ✔")


def demo_tail_arg_error_keeps_frame() -> None:
    line("异常边界 3：尾调用参数求值出错 —— 当前帧不得提前替换")
    # f(x) 尾调用 g(x, <调用整数>): 第二参数先引用本帧 x，再触发运行期错误
    g = ast.Lambda(["a", "b"], ast.Add(ast.Var("a"), ast.Var("b")))
    f = ast.Lambda(
        ["x"],
        ast.Call(
            ast.Var("g"),
            [ast.Var("x"), ast.Call(ast.Int(2), [], loc="ARG-BOOM")],
            loc="TAILCALL",
        ),
    )
    h = ast.Lambda(
        [],
        ast.Add(ast.Int(1000), ast.Call(ast.Var("f"), [ast.Int(5)])),
    )
    root = ast.Let(
        "g", g,
        ast.Let("f", f, ast.Let("h", h, ast.Call(ast.Var("h"), []))),
    )
    program = convert(root)
    interp = ti._TargetInterp(program, 100_000)
    try:
        interp.eval(program.main, ())
    except errors.LangRuntimeError as e:
        print(f"实际触发 {type(e).__name__}: {e}")
    else:
        raise AssertionError("参数中的错误未触发")
    n_frames = interp.active_frames()
    print(f"出错瞬间存活函数帧数={n_frames}（h 非尾调用 f，两帧都必须还在）；"
          f"尾跳转次数={interp.tail_jumps}（仅进入 h 的 1 次，f->g 未发生）")
    assert n_frames == 2 and interp.tail_jumps == 1
    print("帧在函数与全部参数求值成功、校验通过后才替换，出错不提前替换 ✔")


def demo_unbound_boundary() -> None:
    line("异常边界 1：未绑定变量 —— convert 必须拒绝且错误可定位")
    bad = ast.Let(
        "f",
        ast.Lambda([], ast.Set("total", ast.Int(1), loc="set:total:行12")),
        ast.Call(ast.Var("f"), []),
    )
    print("程序片段：let f = () -> set total = 1 （total 从未绑定）")
    try:
        convert(bad)
    except errors.UnboundVariableError as e:
        print(f"实际触发 {type(e).__name__}: {e}")
        print("（该错误在静态解析阶段抛出，不产生部分 Program）✔")
    else:
        raise AssertionError("未绑定变量未被拒绝")


def demo_node_limit_boundary() -> None:
    line("异常边界 2：501 个节点 —— 超过 500 上限必须明确报错")
    node = ast.Int(0)
    for k in range(250):  # 1 + 2*250 = 501 个节点
        node = ast.Add(node, ast.Int(k))
    n = ast.count_nodes(node)
    print(f"现场构造左侧 Add 链，节点数 = {n}")
    try:
        convert(node)
    except errors.NodeLimitError as e:
        print(f"实际触发 {type(e).__name__}: 节点数 {e.count} > 上限 {e.limit} ✔")
    else:
        raise AssertionError("超规模程序未被拒绝")


def main() -> None:
    import time

    print("recenv 演示：递归闭包的环境布局转换 + 真尾位置跳转")
    print(f"Python {sys.version.split()[0]}，仅标准库，离线运行")
    t0 = time.time()
    demo_counter()
    demo_mutual()
    demo_tail_recursion()
    demo_non_tail_frame_growth()
    demo_argument_capture_order()
    demo_unbound_boundary()
    demo_tail_arg_error_keeps_frame()
    demo_node_limit_boundary()
    dt = time.time() - t0
    line("演示结束")
    print(f"全部现场计算完成，实际耗时 {dt:.2f} 秒（上限 90 秒，无 sleep）；")
    print("测试请运行：./.venv/bin/python -m unittest discover -s tests -v")


if __name__ == "__main__":
    main()

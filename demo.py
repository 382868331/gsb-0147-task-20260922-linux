"""固定输入演示（确定性、离线、无第三方依赖）。

运行::

    python demo.py

内容：
1. 正常结果：共享 cell 的计数器闭包 + letrec 互递归 even/odd；
   独立源解释器给出返回值与副作用序列，convert 后打印函数表与显式环境
   布局，目标解释器真实执行并逐项对照。
2. 尾调用执行：交替互尾递归 f/g 跑到 20000（超过通常宿主递归深度），
   峰值活动调用帧数恒为 1；再现场把宿主递归限制压到 200 复跑，证明走的
   是迭代跳转而非抬高宿主栈。对照组的非尾递归帧数随嵌套线性增长。
3. 异常边界：真实构造一个含未绑定变量的程序，convert 必须拒绝并给出
   可定位错误；另真实触发 501 节点的 NodeLimitError。

所有结果均为现场计算，不使用 sleep，不回放预录结果。
"""

from __future__ import annotations

import sys

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source, eval_target, run_program_with_stats


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
    stats = run_program_with_stats(program)

    show_layout(program)
    print()
    print(f"源解释器（独立参考）: even({n}) = {s_value}  副作用={s_events}")
    print(f"目标解释器（执行 IR）: even({n}) = {stats.value}  副作用={stats.events}")
    print(f"  调用计量：峰值活动调用帧={stats.max_call_frames}，"
          f"尾跳转={stats.tail_jumps}，步数={stats.steps}")
    assert (s_value, s_events) == (stats.value, stats.events)
    assert s_value == 0  # 9 是奇数
    # even/odd 的互递归调用位于 if0 的结果分支 -> 尾位置，帧不增长
    assert stats.max_call_frames == 1
    print("对照结果：两者一致，even(9)=0；互递归为尾调用，峰值帧恒为 1 ✔")


def build_alt_tail(n):
    """交替互尾递归：f/g 各自在 if0 结果分支尾调用对方。"""
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


def build_non_tail_sum(n):
    """非尾递归：递归调用处于 Add 操作数位（1 + s(n-1)），必须保留每帧。"""
    body = ast.If0(
        ast.Var("n"), ast.Int(0),
        ast.Add(
            ast.Int(1),
            ast.Call(ast.Var("s"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    return ast.LetRec(
        [ast.RecBinding("s", ast.Lambda(["n"], body))],
        ast.Call(ast.Var("s"), [ast.Int(n)]),
    )


def demo_tail_calls() -> None:
    line("尾调用执行：交替互尾递归 20000 层，活动调用帧数有界")

    big = 20_000
    root = build_alt_tail(big)
    program = convert(root)
    stats = run_program_with_stats(program, step_limit=1_000_000)
    print(f"f/g 交替尾递归 f({big})：结果={stats.value}（偶 -> f(0)=7）")
    print(f"  峰值活动调用帧 = {stats.max_call_frames}（尾调用替换当前帧，不压栈）")
    print(f"  尾跳转次数 = {stats.tail_jumps}（= {big}）")
    assert stats.max_call_frames == 1
    assert stats.tail_jumps == big

    print()
    print("把宿主 Python 递归限制现场压到 200（远低于 20000）后复跑：")
    saved = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        stats2 = run_program_with_stats(program, step_limit=1_000_000)
    finally:
        sys.setrecursionlimit(saved)
    print(f"  结果={stats2.value}，峰值帧={stats2.max_call_frames}，"
          f"尾跳转={stats2.tail_jumps} —— 不依赖宿主递归深度 ✔")
    assert (stats2.value, stats2.max_call_frames) == (7, 1)

    print()
    print("对照组：非尾递归 s(n)=1+s(n-1)，递归调用在 Add 操作数位（非尾位置）")
    nt = build_non_tail_sum(50)
    snt = run_program_with_stats(convert(nt), step_limit=100_000)
    print(f"  s(50) = {snt.value}；峰值活动调用帧 = {snt.max_call_frames}"
          f"（入口 1 + 50 次非尾自调用），尾跳转 = {snt.tail_jumps}")
    assert (snt.value, snt.max_call_frames, snt.tail_jumps) == (50, 51, 0)
    print("  帧数随嵌套真实增长，并未被尾跳转藏掉 ✔")


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
    print("recenv 演示：闭包转换环境布局 + 尾位置跳转执行")
    print(f"Python {__import__('sys').version.split()[0]}，仅标准库，离线运行")
    demo_counter()
    demo_mutual()
    demo_tail_calls()
    demo_unbound_boundary()
    demo_node_limit_boundary()
    line("演示结束")
    print("全部现场计算完成；测试请运行：python -m unittest discover -s tests -v")


if __name__ == "__main__":
    main()

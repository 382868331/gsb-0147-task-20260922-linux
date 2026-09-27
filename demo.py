"""固定输入演示（确定性、离线、无第三方依赖）。

运行::

    python demo.py

内容：
1. 正常结果：共享 cell 的计数器闭包 + letrec 互递归 even/odd
   + 20 万次尾递归只保留 1 层调用现场（FrameStats 实测）；
   独立源解释器给出返回值与副作用序列，convert 后打印函数表与显式环境
   布局，目标解释器真实执行并逐项对照。
2. 异常边界：真实构造一个含未绑定变量的程序，convert 必须拒绝并给出
   可定位错误；另真实触发 501 节点的 NodeLimitError。

所有结果均为现场计算，不使用 sleep，不回放预录结果。
"""

from __future__ import annotations

from recenv import ast_nodes as ast
from recenv import convert, errors, eval_source, eval_target, eval_target_tracked


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


def build_countdown(n: int) -> ast.Expr:
    # letrec loop(n) = if0 n then 0 else loop(n - 1)
    body = ast.If0(
        ast.Var("n"),
        ast.Int(0),
        ast.Call(ast.Var("loop"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
    )
    return ast.LetRec(
        [ast.RecBinding("loop", ast.Lambda(["n"], body))],
        ast.Call(ast.Var("loop"), [ast.Int(n)]),
    )


def demo_tail_call_reuse() -> None:
    line("正常结果 C：尾调用复用调用现场（实际保留的调用层数）")
    n = 200_000
    root = build_countdown(n)
    program = convert(root)
    value, _events, stats = eval_target_tracked(program, step_limit=10_000_000)
    # 小规模同源解释器对照返回值（源解释器保持朴素递归，只跑小 n）
    small_value, _ = eval_source(build_countdown(200))
    print(f"程序：letrec loop(n) = if0 n then 0 else loop(n-1)；调用 loop({n})")
    print(f"返回值：{value}（源解释器小样本 loop(200) = {small_value}，一致）")
    print("目标解释器实际记录的调用层占用：")
    print(f"  总调用次数 calls          = {stats.calls}")
    print(f"  复用现场的尾调用 tail_reuses = {stats.tail_reuses}")
    print(f"  峰值存活激活帧 peak_depth  = {stats.peak_depth}")
    print(f"  结束时存活激活帧 depth     = {stats.depth}")
    assert value == 0 and small_value == 0
    assert stats.peak_depth == 1, stats.peak_depth
    assert stats.tail_reuses == n
    print("结论：20 万次连续尾调用始终只占 1 层调用现场（不是调高递归上限）。")


def main() -> None:
    print("recenv 演示：递归闭包的环境布局转换")
    print(f"Python {__import__('sys').version.split()[0]}，仅标准库，离线运行")
    demo_counter()
    demo_mutual()
    demo_tail_call_reuse()
    demo_unbound_boundary()
    demo_node_limit_boundary()
    line("演示结束")
    print("全部现场计算完成；测试请运行：python -m unittest discover -s tests -v")


if __name__ == "__main__":
    main()

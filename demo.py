"""固定输入演示（确定性、离线、无第三方依赖）。

运行::

    python demo.py

内容：
1. 正常结果：共享 cell 的计数器闭包 + letrec 互递归 even/odd；
   独立源解释器给出返回值与副作用序列，convert 后打印函数表与显式环境
   布局，目标解释器真实执行并逐项对照。
2. 异常边界：真实构造一个含未绑定变量的程序，convert 必须拒绝并给出
   可定位错误；另真实触发 501 节点的 NodeLimitError。

所有结果均为现场计算，不使用 sleep，不回放预录结果。
"""

from __future__ import annotations

from recenv import ast_nodes as ast
from recenv import (
    convert,
    errors,
    eval_source,
    eval_source_with_stats,
    eval_target,
    eval_target_with_stats,
)


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


def build_tail_countdown(n: int) -> ast.Expr:
    # letrec loop(n, acc) = if0 n then acc else loop(n + -1, acc + 1)
    # in loop(n, 0)   —— 完全尾递归
    loop = ast.Lambda(
        ["n", "acc"],
        ast.If0(
            ast.Var("n"),
            ast.Var("acc"),
            ast.Call(
                ast.Var("loop"),
                [
                    ast.Add(ast.Var("n"), ast.Int(-1)),
                    ast.Add(ast.Var("acc"), ast.Int(1)),
                ],
            ),
        ),
    )
    return ast.LetRec(
        [ast.RecBinding("loop", loop)],
        ast.Call(ast.Var("loop"), [ast.Int(n), ast.Int(0)]),
    )


def demo_tail_call_reuse() -> None:
    line("正常结果 C：深层尾递归 —— 目标解释器复用调用现场，保留层数为常数")
    n = 8000
    root = build_tail_countdown(n)
    program = convert(root)
    s_value, s_events, s_stats = eval_source_with_stats(root, step_limit=200_000)
    t_value, t_events, t_stats = eval_target_with_stats(program, step_limit=200_000)

    print(f"程序：letrec loop(n, acc) = if0 n then acc else loop(n-1, acc+1)，"
          f"loop({n}, 0)")
    print(f"源解释器（对照，无复用）: 返回值={s_value}  "
          f"实际保留的最大调用层数={s_stats.max_call_depth}")
    print(f"目标解释器（尾调用复用）: 返回值={t_value}  "
          f"实际保留的最大调用层数={t_stats.max_call_depth}")
    assert (s_value, s_events) == (t_value, t_events)
    assert t_stats.max_call_depth <= 2 < s_stats.max_call_depth
    print("对照结果：返回值与副作用序列一致；目标侧保留层数为常数，")
    print("减少的是运行占用（实际调用层数），而非调高递归上限 ✔")


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

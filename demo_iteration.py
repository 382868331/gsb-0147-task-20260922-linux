"""本次迭代的 partial 应用现场演示（仅标准库、无 sleep、无预录）。

运行：``python demo_iteration.py``
"""

from __future__ import annotations

from dataclasses import fields

from recenv import ast_nodes as ast
from recenv import convert, eval_source, eval_target
from recenv import source_interp as si
from recenv import target_interp as ti


def title(text: str) -> None:
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def run_pair(root: ast.Expr):
    source_value, source_events = eval_source(root)
    program = convert(root)
    target_value, target_events = eval_target(program)

    source_steps = si._SrcInterp(10_000)
    source_steps.eval(root, None)
    target_steps = ti._TargetInterp(program, 10_000)
    target_steps.eval(program.main, ())

    return (
        (source_value, source_events, source_steps.steps),
        (target_value, target_events, target_steps.steps),
        program,
    )


def once_bound_program() -> ast.Expr:
    # p = partial(f, set c=10)；p 被复用两次，但前缀值只在 partial 时求一次。
    return ast.Let(
        "c",
        ast.Int(0),
        ast.Let(
            "f",
            ast.Lambda(["a", "b"], ast.Add(ast.Var("a"), ast.Var("b"))),
            ast.Let(
                "p",
                ast.Partial(ast.Var("f"), [ast.Set("c", ast.Int(10))]),
                ast.Add(
                    ast.Call(ast.Var("p"), [ast.Int(1)]),
                    ast.Add(
                        ast.Call(ast.Var("p"), [ast.Int(2)]),
                        ast.Var("c"),
                    ),
                ),
            ),
        ),
    )


def over_application_program() -> ast.Expr:
    # partial(curry-ish, 1, 2, 3, 4)：收齐前两个参数进入外层函数，
    # 多出的 3、4 从左到右应用到返回函数。
    return ast.Let(
        "f",
        ast.Lambda(
            ["a", "b"],
            ast.Lambda(
                ["c", "d"],
                ast.Add(
                    ast.Add(ast.Var("a"), ast.Var("b")),
                    ast.Add(ast.Var("c"), ast.Var("d")),
                ),
            ),
        ),
        ast.Partial(ast.Var("f"), [ast.Int(1), ast.Int(2), ast.Int(3), ast.Int(4)]),
    )


def zero_partial_boundary_program() -> ast.Expr:
    # 零参 partial 返回等价闭包；这里最后不调用它，证明函数体未提前执行。
    return ast.Let(
        "c",
        ast.Int(0),
        ast.Let(
            "p",
            ast.Partial(
                ast.Lambda(["a", "b"], ast.Set("c", ast.Int(99))),
                [],
            ),
            ast.Var("c"),
        ),
    )


def show(name: str, root: ast.Expr) -> None:
    title(name)
    source, target, program = run_pair(root)
    print(f"IR 中 partial 节点数量：{_count_tpartial(program.main)}")
    print(f"参考解释器：(返回值, 副作用, 步数) = {source}")
    print(f"目标解释器：(返回值, 副作用, 步数) = {target}")
    assert source == target
    print("两项返回值、副作用记录和步数逐项一致 ✔")


def _count_tpartial(expr: ast.TExpr) -> int:
    if isinstance(expr, ast.TPartial):
        return 1 + sum(_count_children(value) for value in fields(expr))
    return _count_children(expr)


def _count_children(value: object) -> int:
    total = 1 if isinstance(value, ast.TPartial) else 0
    if hasattr(value, "__dataclass_fields__"):
        for item in fields(value):
            total += _count_children(getattr(value, item.name))
    elif isinstance(value, tuple):
        for item in value:
            total += _count_children(item)
    return total


def show_non_function_boundary() -> None:
    title("边界：过量参数应用到非函数时，先完成已发生实参与函数体副作用")
    root = ast.Let(
        "c",
        ast.Int(0),
        ast.Partial(
            ast.Lambda(
                ["a", "b"],
                ast.Set("c", ast.Add(ast.Var("a"), ast.Var("b"))),
            ),
            [
                ast.Set("c", ast.Int(1)),
                ast.Set("c", ast.Int(2)),
                ast.Set("c", ast.Int(3)),
            ],
        ),
    )
    source = si._SrcInterp(10_000)
    program = convert(root)
    target = ti._TargetInterp(program, 10_000)
    try:
        source.eval(root, None)
    except Exception as source_error:
        source_type = type(source_error).__name__
    try:
        target.eval(program.main, ())
    except Exception as target_error:
        target_type = type(target_error).__name__
    print(f"参考解释器异常：{source_type}，已记录副作用：{source.events}")
    print(f"目标解释器异常：{target_type}，已记录副作用：{target.events}")
    assert source_type == target_type == "LangRuntimeError"
    assert source.events == target.events
    print("检查发生在函数体和全部当前实参副作用之后，两侧顺序一致 ✔")


def main() -> None:
    print("recenv 本次迭代：闭包 partial 应用")
    print(f"Python {__import__('sys').version.split()[0]}；所有结果均现场计算")
    show("前缀绑定只求值一次，并返回可复用普通闭包", once_bound_program())
    show("参数过量时把剩余参数从左到右应用到返回函数", over_application_program())
    show("零参 partial 返回等价闭包，不提前执行函数体", zero_partial_boundary_program())
    show_non_function_boundary()
    title("演示结束")


if __name__ == "__main__":
    main()

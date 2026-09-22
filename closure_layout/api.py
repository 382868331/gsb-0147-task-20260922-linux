"""公开门面接口：parse / convert / run_source / run_target / run。

典型用法::

    from closure_layout import parse, convert, run

    program = convert(parse("(let (x 1) ((lambda () (+ x 2))))"))
    print(run(program).value)          # 3
"""

from __future__ import annotations

from . import ast, source_ref, target as target_mod
from .convert import convert as _convert
from .ir import Function, Program
from .reader import parse as _parse
from .errors import ClosureLayoutError  # re-export 用

__all__ = [
    "parse",
    "convert",
    "run_source",
    "run_target",
    "run",
    "Program",
    "Function",
    "ClosureLayoutError",
]


def parse(text: str) -> ast.Node:
    """解析 s 表达式源码为 AST（已结构校验）。"""
    return _parse(text)


def convert(root: ast.Node) -> Program:
    """环境布局转换：AST -> 无自由变量函数表 + 显式环境 IR。

    详见 :mod:`closure_layout.convert` 与 :mod:`closure_layout.ir`。
    """
    return _convert(root)


def run_source(root: ast.Node, *, max_steps: int = 10_000) -> source_ref.Result:
    """用独立源参考解释器直接执行 AST（不经过转换核心）。"""
    ast.validate(root)
    return source_ref.run(root, max_steps=max_steps)


def run_target(program: Program, *, max_steps: int = 10_000) -> target_mod.Result:
    """执行转换后的 IR 程序。"""
    return target_mod.run(program, max_steps=max_steps)


def run(program: Program, *, max_steps: int = 10_000) -> target_mod.Result:
    """``run_target`` 的简称。"""
    return target_mod.run(program, max_steps=max_steps)

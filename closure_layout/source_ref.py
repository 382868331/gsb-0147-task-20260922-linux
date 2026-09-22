"""独立源参考解释器（不依赖 scope/convert/target 任何被测核心）。

本模块只把 :mod:`closure_layout.ast` 的节点当数据，自行实现词法环境、
set 编号与求值，用于和目标解释器的返回值、副作用序列做独立对照。

环境模型（教科书式）：每个绑定是一个可变槽；lambda 闭包捕获其创建
时的环境链；letrec 用一个所有成员共享、初始为“未初始化”的帧同时
绑定。这与转换后“cell + 共享组帧”的布局在可观察行为上等价，
实现上互不共用代码。

set 编号：转换核心在结构深度优先遍历中按进入 Set 节点的顺序编号；
本解释器用完全独立写出的同一次序规则（Add 左右、Call 函数后实参、
If0 条件/两分支、Let 右值后主体、letrec 成员函数体后组体）编号，
使两侧副作用轨迹可按编号逐项对照。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import ast
from .errors import (
    ArityError,
    LangTypeError,
    StepLimitError,
    UnboundVariableError,
    UninitializedSlotError,
)

# 求值期间临时提高递归上限所需的值（10000 步内深调用 + 表达式嵌套）。
_EVAL_RECURSION_LIMIT = 100_000

DEFAULT_STEP_LIMIT = 10_000
_UNINIT = object()
# 与目标解释器约定的非整数赋值标记（两侧各自独立定义，数值一致）。
NONINT_TRACE = -(10**100)


@dataclass(slots=True)
class _Slot:
    val: object = _UNINIT


@dataclass(slots=True)
class _Env:
    vars: dict[str, _Slot]
    parent: "_Env | None"

    def find(self, name: str, node: ast.Node) -> _Slot:
        env: _Env | None = self
        while env is not None:
            slot = env.vars.get(name)
            if slot is not None:
                return slot
            env = env.parent
        pos = node.pos
        raise UnboundVariableError(
            f"未绑定的变量 {name!r}",
            pos[0] if pos else None, pos[1] if pos else None)


@dataclass(slots=True)
class _Closure:
    params: tuple[str, ...]
    body: ast.Node
    env: _Env
    name: str


@dataclass(slots=True)
class Result:
    value: int
    steps: int
    trace: list[tuple[int, int]]


def _number_sets(root: ast.Node) -> dict[int, int]:
    """独立按结构次序给每个 set 表达式编号（次序约定见模块文档）。"""
    tags: dict[int, int] = {}
    counter = 0

    def walk(n: ast.Node) -> None:
        nonlocal counter
        if isinstance(n, ast.Int) or isinstance(n, ast.Var):
            return
        if isinstance(n, ast.Add):
            walk(n.lhs)
            walk(n.rhs)
        elif isinstance(n, ast.Set):
            tags[id(n)] = counter
            counter += 1
            walk(n.value)
        elif isinstance(n, ast.If0):
            walk(n.cond)
            walk(n.then)
            walk(n.els)
        elif isinstance(n, ast.Call):
            walk(n.func)
            for a in n.args:
                walk(a)
        elif isinstance(n, ast.Let):
            walk(n.bound)
            walk(n.body)
        elif isinstance(n, ast.Lambda):
            walk(n.body)
        elif isinstance(n, ast.Letrec):
            for _name, lam in n.bindings:
                walk(lam)
            walk(n.body)
        else:  # pragma: no cover
            raise LangTypeError(f"参考解释器遇到未知节点 {type(n).__name__}")

    walk(root)
    return tags


def _static_check(root: ast.Node) -> None:
    """执行前独立词法检查：任何未绑定引用（含死分支）都静态报错。

    与核心的 scope.py 完全独立实现：只做一遍名字栈遍历。
    """

    def unbound(name: str, node: ast.Node) -> UnboundVariableError:
        pos = node.pos
        return UnboundVariableError(
            f"未绑定的变量 {name!r}",
            pos[0] if pos else None, pos[1] if pos else None)

    def check(n: ast.Node, stack: list[set[str]]) -> None:
        if isinstance(n, (ast.Int,)):
            return
        if isinstance(n, ast.Var):
            if not any(n.name in s for s in stack):
                raise unbound(n.name, n)
        elif isinstance(n, ast.Add):
            check(n.lhs, stack)
            check(n.rhs, stack)
        elif isinstance(n, ast.Set):
            if not any(n.name in s for s in stack):
                raise unbound(n.name, n)
            check(n.value, stack)
        elif isinstance(n, ast.If0):
            check(n.cond, stack)
            check(n.then, stack)
            check(n.els, stack)
        elif isinstance(n, ast.Call):
            check(n.func, stack)
            for a in n.args:
                check(a, stack)
        elif isinstance(n, ast.Let):
            check(n.bound, stack)
            check(n.body, stack + [{n.name}])
        elif isinstance(n, ast.Lambda):
            dup = set()
            ps: set[str] = set()
            for p in n.params:
                if p in dup:
                    raise LangTypeError(f"lambda 形参重名 {p!r}")
                dup.add(p)
                ps.add(p)
            check(n.body, stack + [ps])
        elif isinstance(n, ast.Letrec):
            frame = {name for name, _lam in n.bindings}
            for _name, lam in n.bindings:
                ps = set(lam.params)
                check(lam.body, stack + [frame, ps])
            check(n.body, stack + [frame])
        else:  # pragma: no cover
            raise LangTypeError(f"参考解释器遇到未知节点 {type(n).__name__}")

    check(root, [])


def run(root: ast.Node, *, max_steps: int = DEFAULT_STEP_LIMIT) -> Result:
    """直接解释源 AST，返回值与副作用轨迹（不经过转换）。"""
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) \
            or max_steps <= 0:
        raise ValueError("max_steps 必须是正整数")

    # 与编译器对称的静态前置：节点上限 + 未绑定变量（独立实现）。
    from .ast import MAX_NODES, count_nodes
    from .errors import NodeLimitError
    n_nodes = count_nodes(root)
    if n_nodes > MAX_NODES:
        pos = root.pos
        raise NodeLimitError(
            f"AST 节点数 {n_nodes} 超过上限 {MAX_NODES}",
            pos[0] if pos else None, pos[1] if pos else None)
    _static_check(root)

    tags = _number_sets(root)
    trace: list[tuple[int, int]] = []
    steps = 0

    def budget() -> None:
        nonlocal steps
        steps += 1
        if steps > max_steps:
            raise StepLimitError(
                f"源解释器执行步数超过预算 {max_steps}（已执行 {steps} 步）；"
                "这是预算耗尽，不代表程序无解或无界")

    def as_int(v: object, what: str) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise LangTypeError(f"{what}不是整数")
        return v

    def eval_(n: ast.Node, env: _Env) -> object:
        budget()
        if isinstance(n, ast.Int):
            return n.value
        if isinstance(n, ast.Var):
            slot = env.find(n.name, n)
            if slot.val is _UNINIT:
                raise UninitializedSlotError(
                    f"变量 {n.name!r} 所在的 letrec 槽尚未初始化",
                    n.pos[0] if n.pos else None,
                    n.pos[1] if n.pos else None,
                    during_initialization=True)
            return slot.val
        if isinstance(n, ast.Add):
            l = as_int(eval_(n.lhs, env), "+ 左操作数")
            r = as_int(eval_(n.rhs, env), "+ 右操作数")
            return l + r
        if isinstance(n, ast.Set):
            v = eval_(n.value, env)
            slot = env.find(n.name, n)
            slot.val = v
            trace.append((tags[id(n)],
                          v if isinstance(v, int) and not isinstance(v, bool)
                          else NONINT_TRACE))
            return v
        if isinstance(n, ast.If0):
            c = as_int(eval_(n.cond, env), "if0 条件")
            return eval_(n.then if c == 0 else n.els, env)
        if isinstance(n, ast.Lambda):
            return _Closure(n.params, n.body, env, "lambda")
        if isinstance(n, ast.Call):
            fval = eval_(n.func, env)
            args = [eval_(a, env) for a in n.args]
            if not isinstance(fval, _Closure):
                raise LangTypeError("调用对象不是函数")
            if len(args) != len(fval.params):
                raise ArityError(
                    f"函数需要 {len(fval.params)} 个实参，"
                    f"得到 {len(args)} 个")
            call_env = _Env(dict(zip(fval.params,
                                     (_Slot(a) for a in args))),
                            fval.env)
            return eval_(fval.body, call_env)
        if isinstance(n, ast.Let):
            v = eval_(n.bound, env)
            # 右值在扩展前求值，故 let 不允许自引用递归。
            return eval_(n.body, _Env({n.name: _Slot(v)}, env))
        if isinstance(n, ast.Letrec):
            frame = _Env({name: _Slot() for name, _lam in n.bindings}, env)
            # 同时绑定：成员闭包全部捕获尚未填充完毕的共享帧；
            # 仅“调用”发生在填充之后，故初始化体内互相调用安全。
            for name, lam in n.bindings:
                frame.vars[name].val = _Closure(
                    lam.params, lam.body, frame, name)
            return eval_(n.body, frame)
        raise LangTypeError(f"参考解释器遇到未知节点 {type(n).__name__}")  # pragma: no cover

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, _EVAL_RECURSION_LIMIT))
    try:
        result = eval_(root, _Env({}, None))
    finally:
        sys.setrecursionlimit(old_limit)
    if isinstance(result, bool) or not isinstance(result, int):
        raise LangTypeError("程序顶层结果必须是整数")
    return Result(value=result, steps=steps, trace=trace)

"""独立源解释器（参考实现）。

刻意使用与转换器/目标解释器不同的实现策略：名字作用域用链表帧
``dict[name] -> Box``，闭包直接捕获帧指针。它*不*导入
:mod:`recenv.convert`，期望值由本解释器独立给出；它与目标解释器的
一致结果只作为补充校验。

副作用序列：语言唯一的可观察副作用是 ``set`` 赋值，记录为
``("set", 名字, 写入的整数)``，按发生顺序排列。

严格从左到右：call 先求函数再依次求实参；add 先左后右；let 先求值再入作用域。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import LangRuntimeError, ScopeError, StepLimitError, UnboundVariableError

# 解释器按 AST 递归下降；步数上限 10000 时嵌套可能接近该量级，
# 提升 Python 栈余量以保证先触发步数预算而不是 RecursionError。
sys.setrecursionlimit(max(100_000, sys.getrecursionlimit()))

DEFAULT_STEP_LIMIT = 10_000


class Box:
    __slots__ = ("value", "filled")

    def __init__(self, value=None, filled: bool = True) -> None:
        self.value = value
        self.filled = filled


@dataclass
class SrcClosure:
    params: tuple[str, ...]
    body: ast.Expr
    env: "Scope"


def type_name(v: object) -> str:
    """语言层面的类型名（错误消息用，不暴露解释器内部类名）。"""
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, SrcClosure):
        return "function"
    return type(v).__name__


class Scope:
    __slots__ = ("names", "parent")

    def __init__(self, parent: "Scope | None") -> None:
        self.names: dict[str, Box] = {}
        self.parent = parent

    def declare(self, name: str, box: Box) -> None:
        self.names[name] = box

    def resolve_box(self, name: str) -> Box:
        s: Scope | None = self
        while s is not None:
            if name in s.names:
                return s.names[name]
            s = s.parent
        raise UnboundVariableError(name)


def evaluate(
    root: ast.Expr, step_limit: int = DEFAULT_STEP_LIMIT
) -> tuple[int, list[tuple[str, str, int]]]:
    """求值源 AST，返回 ``(整数结果, 副作用事件序列)``。

    Raises:
        UnboundVariableError: 变量未绑定
        LangRuntimeError: 类型/元数/调用非函数等运行期错误
        StepLimitError: 超过 step_limit 个求值步（携带已产生的事件）
    """
    interp = _SrcInterp(step_limit)
    try:
        value = interp.eval(root, None)
    except StepLimitError as e:
        e.events = list(interp.events)
        raise
    if not isinstance(value, int) or isinstance(value, bool):
        raise LangRuntimeError(f"program result must be an integer, got {type(value).__name__}")
    return value, interp.events


class _SrcInterp:
    def __init__(self, step_limit: int) -> None:
        self.steps = 0
        self.limit = step_limit
        self.events: list[tuple[str, str, int]] = []

    def tick(self) -> None:
        self.steps += 1
        if self.steps > self.limit:
            raise StepLimitError(self.steps, self.limit)

    def eval(self, node: ast.Expr, env: Scope | None) -> object:
        self.tick()
        if isinstance(node, ast.Int):
            return node.value
        if isinstance(node, ast.Var):
            if env is None:
                raise UnboundVariableError(node.name, node.loc)
            box = env.resolve_box(node.name)
            if not box.filled:
                raise LangRuntimeError(
                    f"read of recursively-bound name {node.name!r} before initialization"
                )
            return box.value
        if isinstance(node, ast.Add):
            a = self._as_int(self.eval(node.lhs, env), node.loc)
            b = self._as_int(self.eval(node.rhs, env), node.loc)
            return a + b
        if isinstance(node, ast.Let):
            v = self.eval(node.value, env)
            inner = Scope(env)
            inner.declare(node.name, Box(v))
            return self.eval(node.body, inner)
        if isinstance(node, ast.Lambda):
            return SrcClosure(tuple(node.params), node.body, env if env is not None else Scope(None))
        if isinstance(node, ast.Call):
            callee = self.eval(node.func, env)
            args = [self.eval(a, env) for a in node.args]
            return self.apply(callee, args, node.loc)
        if isinstance(node, ast.If0):
            c = self._as_int(self.eval(node.cond, env), node.loc)
            return self.eval(node.then_e if c == 0 else node.else_e, env)
        if isinstance(node, ast.Set):
            v = self.eval(node.value, env)
            v = self._as_int(v, node.loc)
            if env is None:
                raise UnboundVariableError(node.name, node.loc)
            target = env.resolve_box(node.name)
            if not target.filled:
                raise ScopeError(
                    f"cannot assign letrec function slot {node.name!r}",
                )
            target.value = v
            self.events.append(("set", node.name, v))
            return v
        if isinstance(node, ast.LetRec):
            group = Scope(env)
            boxes: list[Box] = []
            for rb in node.bindings:
                box = Box(filled=False)
                boxes.append(box)
                group.declare(rb.name, box)
            # 同时绑定：先构造全部闭包（共享 group），再按声明顺序填充
            closures = [
                self.eval(rb.func, group) for rb in node.bindings
            ]
            for box, clo in zip(boxes, closures):
                if not isinstance(clo, SrcClosure):
                    raise LangRuntimeError("letrec binding must evaluate to a function")
                box.value = clo
                box.filled = True
            return self.eval(node.body, group)
        raise LangRuntimeError(f"cannot evaluate node {type(node).__name__}")

    def apply(self, callee: object, args: list[object], loc: str | None) -> object:
        prefix = f"{loc}: " if loc else ""
        if not isinstance(callee, SrcClosure):
            raise LangRuntimeError(f"{prefix}attempt to call non-function value")
        if len(args) != len(callee.params):
            raise LangRuntimeError(
                f"{prefix}function expects {len(callee.params)} args, got {len(args)}"
            )
        call_scope = Scope(callee.env)
        for name, a in zip(callee.params, args):
            call_scope.declare(name, Box(self._as_int_or_fn(a, loc)))
        return self.eval(callee.body, call_scope)

    @staticmethod
    def _as_int(v: object, loc: str | None) -> int:
        prefix = f"{loc}: " if loc else ""
        if isinstance(v, bool) or not isinstance(v, int):
            raise LangRuntimeError(
                f"{prefix}integer expected, got {type_name(v)}"
            )
        return v

    @staticmethod
    def _as_int_or_fn(v: object, loc: str | None) -> object:
        if isinstance(v, (int, SrcClosure)) and not isinstance(v, bool):
            return v
        prefix = f"{loc}: " if loc else ""
        raise LangRuntimeError(f"{prefix}bad argument value: {type_name(v)}")

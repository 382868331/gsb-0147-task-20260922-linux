"""源语言 AST 的公开数据结构与校验。

节点均为不可变 dataclass，可用构造器手工搭建，也可由 :func:`closure_layout.parse`
从 s 表达式读入。``pos`` 为 ``(line, column)``（均 1 起始）或 ``None``，
仅用于错误定位，不参与相等比较以外的任何语义。

源语言文法（``E`` 为表达式，``x`` 为标识符）::

    E  := 整数
        |  (+ E E)
        |  x
        |  (set x E)
        |  (if0 E E E)
        |  (lambda (x ...) E)
        |  (E E ...)
        |  (let (x E) E)
        |  (letrec ((x (lambda (x ...) E)) ...) E)

求值顺序一律严格从左到右；letrec 的绑定同时建立（互递归），
各绑定右值必须是 lambda。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Union

from .errors import ValidationError

Pos = Union[tuple[int, int], None]

MAX_NODES = 500

# 标识符允许的形式：字母/下划线开头，后接字母数字/_/-/?!
# 括号、引号、空白为 s 表达式语法字符，不允许出现在标识符中。
_KEYWORDS = frozenset({"+", "set", "if0", "lambda", "let", "letrec"})


@dataclass(frozen=True, slots=True)
class Node:
    pos: Pos = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Int(Node):
    value: int = 0


@dataclass(frozen=True, slots=True)
class Add(Node):
    lhs: Node = None  # type: ignore[assignment]
    rhs: Node = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class Var(Node):
    name: str = ""


@dataclass(frozen=True, slots=True)
class Set(Node):
    name: str = ""
    value: Node = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class If0(Node):
    cond: Node = None  # type: ignore[assignment]
    then: Node = None  # type: ignore[assignment]
    els: Node = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class Lambda(Node):
    params: tuple[str, ...] = ()
    body: Node = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class Call(Node):
    func: Node = None  # type: ignore[assignment]
    args: tuple[Node, ...] = ()


@dataclass(frozen=True, slots=True)
class Let(Node):
    name: str = ""
    bound: Node = None  # type: ignore[assignment]
    body: Node = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class Letrec(Node):
    # 每项为 (名字, lambda 节点)；lambda 形式要求在校验阶段检查。
    bindings: tuple[tuple[str, Lambda], ...] = ()
    body: Node = None  # type: ignore[assignment]


# ---------------------------------------------------------------- 便捷构造器

def _pos_of(pos: Pos) -> Pos:
    if pos is None:
        return None
    if (not isinstance(pos, tuple) or len(pos) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool)
                       and v >= 1 for v in pos)):
        raise ValidationError("pos 必须是 (行, 列) 正整数二元组")
    return pos


def int_(value: int, pos: Pos = None) -> Int:
    """整数字面量。拒绝 bool；非 int（含 float/NaN/Infinity）一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and math.isnan(value):
            raise ValidationError("数值不能为 NaN")
        if isinstance(value, float) and math.isinf(value):
            raise ValidationError("数值不能为 Infinity/-Infinity")
        raise ValidationError(f"整数节点需要 int（拒绝 bool），得到 {type(value).__name__}")
    return Int(pos=_pos_of(pos), value=value)


def add_(lhs: Node, rhs: Node, pos: Pos = None) -> Add:
    return Add(pos=_pos_of(pos), lhs=lhs, rhs=rhs)


def var_(name: str, pos: Pos = None) -> Var:
    return Var(pos=_pos_of(pos), name=name)


def set_(name: str, value: Node, pos: Pos = None) -> Set:
    return Set(pos=_pos_of(pos), name=name, value=value)


def if0_(cond: Node, then: Node, els: Node, pos: Pos = None) -> If0:
    return If0(pos=_pos_of(pos), cond=cond, then=then, els=els)


def lambda_(params: tuple[str, ...] | list[str], body: Node,
            pos: Pos = None) -> Lambda:
    return Lambda(pos=_pos_of(pos), params=tuple(params), body=body)


def call_(func: Node, args: tuple[Node, ...] | list[Node],
          pos: Pos = None) -> Call:
    return Call(pos=_pos_of(pos), func=func, args=tuple(args))


def let_(name: str, bound: Node, body: Node, pos: Pos = None) -> Let:
    return Let(pos=_pos_of(pos), name=name, bound=bound, body=body)


def letrec_(bindings: tuple[tuple[str, Lambda], ...] | list[tuple[str, Lambda]],
            body: Node, pos: Pos = None) -> Letrec:
    return Letrec(pos=_pos_of(pos), bindings=tuple(bindings), body=body)


# ---------------------------------------------------------------- 校验

def _err(msg: str, node: Node | None) -> ValidationError:
    pos = node.pos if node is not None else None
    return ValidationError(msg, pos[0] if pos else None,
                           pos[1] if pos else None)


def _check_name(name: object, node: Node, what: str) -> None:
    if not isinstance(name, str) or not name:
        raise _err(f"{what}必须是非空字符串", node)


def validate(node: object) -> None:
    """对整棵 AST 做结构与词法作用域前置校验；非法即抛 :class:`ValidationError`。

    检查项：节点类型、子节点均为 Node、名字为非空字符串、lambda/letrec
    无重复绑定名、letrec 右值必须是 lambda、参数名不得与关键字冲突。
    变量是否“已绑定”不在此处检查（由作用域分析/解释器负责）。
    """

    seen_ids: set[int] = set()

    def walk(n: object, bound: tuple[str, ...]) -> None:
        if not isinstance(n, Node):
            raise ValidationError(f"期望 AST 节点，得到 {type(n).__name__}")
        if id(n) in seen_ids:
            raise _err("AST 节点被复用：每棵树必须由独立节点构成", n)
        seen_ids.add(id(n))

        if isinstance(n, Int):
            if isinstance(n.value, bool) or not isinstance(n.value, int):
                raise _err("整数字面量必须是 int（拒绝 bool）", n)
        elif isinstance(n, Add):
            walk(n.lhs, bound)
            walk(n.rhs, bound)
        elif isinstance(n, Var):
            _check_name(n.name, n, "变量名")
        elif isinstance(n, Set):
            _check_name(n.name, n, "赋值目标")
            walk(n.value, bound)
        elif isinstance(n, If0):
            walk(n.cond, bound)
            walk(n.then, bound)
            walk(n.els, bound)
        elif isinstance(n, Lambda):
            if not isinstance(n.params, tuple) or not all(
                    isinstance(p, str) and p for p in n.params):
                raise _err("lambda 形参必须是非空字符串组成的元组", n)
            dup = _first_duplicate(n.params)
            if dup is not None:
                raise _err(f"lambda 形参重名: {dup!r}", n)
            for p in n.params:
                if p in _KEYWORDS:
                    raise _err(f"绑定名不能使用关键字 {p!r}", n)
            walk(n.body, bound + n.params)
        elif isinstance(n, Call):
            walk(n.func, bound)
            if not isinstance(n.args, tuple):
                raise _err("call 实参必须是元组", n)
            for a in n.args:
                walk(a, bound)
        elif isinstance(n, Let):
            _check_name(n.name, n, "let 绑定名")
            if n.name in _KEYWORDS:
                raise _err(f"绑定名不能使用关键字 {n.name!r}", n)
            walk(n.bound, bound)
            walk(n.body, bound + (n.name,))
        elif isinstance(n, Letrec):
            if not isinstance(n.bindings, tuple) or not n.bindings:
                raise _err("letrec 至少需要一个绑定", n)
            names: list[str] = []
            for name, lam in n.bindings:
                _check_name(name, n, "letrec 绑定名")
                if name in _KEYWORDS:
                    raise _err(f"绑定名不能使用关键字 {name!r}", n)
                if not isinstance(lam, Lambda):
                    from .errors import LetrecFormError
                    pos = n.pos
                    raise LetrecFormError(
                        f"letrec 绑定 {name!r} 的右值必须是 lambda，"
                        f"得到 {type(lam).__name__}",
                        pos[0] if pos else None, pos[1] if pos else None)
                names.append(name)
            dup = _first_duplicate(tuple(names))
            if dup is not None:
                raise _err(f"letrec 绑定重名: {dup!r}", n)
            inner = bound + tuple(names)
            for _, lam in n.bindings:
                walk(lam, inner)
            walk(n.body, inner)
        else:  # pragma: no cover - 节点类型封闭
            raise _err(f"未知 AST 节点类型 {type(n).__name__}", n)

    walk(node, ())


def _first_duplicate(items: tuple[str, ...]) -> str | None:
    seen: set[str] = set()
    for it in items:
        if it in seen:
            return it
        seen.add(it)
    return None


def count_nodes(node: Node) -> int:
    """统计 AST 中表达式节点的数量（letrec 节点与其 lambda 各计一次）。"""
    if isinstance(node, Int) or isinstance(node, Var):
        return 1
    if isinstance(node, Add):
        return 1 + count_nodes(node.lhs) + count_nodes(node.rhs)
    if isinstance(node, Set):
        return 1 + count_nodes(node.value)
    if isinstance(node, If0):
        return 1 + count_nodes(node.cond) + count_nodes(node.then) + count_nodes(node.els)
    if isinstance(node, Lambda):
        return 1 + count_nodes(node.body)
    if isinstance(node, Call):
        return 1 + count_nodes(node.func) + sum(count_nodes(a) for a in node.args)
    if isinstance(node, Let):
        return 1 + count_nodes(node.bound) + count_nodes(node.body)
    if isinstance(node, Letrec):
        return (1 + sum(count_nodes(lam) for _, lam in node.bindings)
                + count_nodes(node.body))
    raise ValidationError(f"无法统计非 AST 节点 {type(node).__name__}")

"""静态解析：绑定 ID、词法遮蔽、可变性分析、自由变量收集。

绑定 ID 在进入作用域时按声明顺序分配（letrec 组内连续分配），
因此自由变量按绑定 ID 排序得到的是确定的、与遮蔽无关的布局顺序——
同名绑定永远不会因名字字符串相同而被混淆。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import ScopeError, UnboundVariableError


@dataclass(frozen=True)
class Binding:
    bid: int
    name: str
    kind: str  # 'let' | 'param' | 'rec'
    mutable: bool = False


@dataclass
class Resolved:
    bindings: dict[int, Binding]
    # id(Lambda 节点) -> 该 lambda 的信息
    lambdas: dict[int, "LambdaInfo"]
    # id(Var/Set 节点) -> 目标绑定 ID
    targets: dict[int, int]
    # id(Let 节点) -> 绑定 ID
    let_bids: dict[int, int]
    # id(LetRec 节点) -> [(名字, 绑定 ID), ...]（声明顺序）
    rec_groups: dict[int, list[tuple[str, int]]]
    next_id: int


@dataclass
class LambdaInfo:
    free: tuple[int, ...]          # 排序后的自由绑定 ID
    param_bids: tuple[int, ...]    # 参数绑定 ID（按声明顺序）
    node: ast.Lambda
    ver_tag: str | None = None    # 该 lambda 自己的版本标签
    group: str | None = None      # letrec 组成员携带的组标签
    member: str | None = None     # 该 lambda 在带版本组中的成员名
    group_bids: tuple[int, ...] = ()  # 所属 letrec 组的成员绑定 ID（声明顺序）


class _Resolver:
    def __init__(self) -> None:
        self.scopes: list[dict[str, Binding]] = []
        self.bindings: dict[int, Binding] = {}
        self.lambdas: dict[int, LambdaInfo] = {}
        self.targets: dict[int, int] = {}
        self.let_bids: dict[int, int] = {}
        self.rec_groups: dict[int, list[tuple[str, int]]] = {}
        self._next = 0

    def _alloc(self, name: str, kind: str) -> Binding:
        b = Binding(self._next, name, kind, mutable=False)
        self._next += 1
        self.bindings[b.bid] = b
        return b

    def _find(self, name: str, loc: str | None) -> Binding:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise UnboundVariableError(name, loc)

    def _mark_mutable(self, b: Binding, loc: str | None) -> None:
        prefix = f"{loc}: " if loc else ""
        if b.kind == "rec":
            raise ScopeError(
                f"{prefix}cannot reassign letrec-bound function {b.name!r}; "
                f"use an enclosing 'let' for mutable state"
            )
        stored = self.bindings[b.bid]
        if not stored.mutable:
            self.bindings[b.bid] = Binding(
                stored.bid, stored.name, stored.kind, mutable=True
            )

    # 返回子表达式中“未在子树内部绑定”的绑定 ID 集合
    def visit(self, node: ast.Expr) -> set[int]:
        if isinstance(node, ast.Int):
            return set()
        if isinstance(node, ast.Var):
            b = self._find(node.name, node.loc)
            self.targets[id(node)] = b.bid
            return {b.bid}
        if isinstance(node, ast.Add):
            left = self.visit(node.lhs)
            return left | self.visit(node.rhs)
        if isinstance(node, ast.Let):
            # 非递归 let：value 看不到新名字；严格先求值 value（先遍历之）
            value_refs = self.visit(node.value)
            b = self._alloc(node.name, "let")
            self.let_bids[id(node)] = b.bid
            self.scopes.append({node.name: b})
            try:
                body_refs = self.visit(node.body)
            finally:
                self.scopes.pop()
            return value_refs | (body_refs - {b.bid})
        if isinstance(node, ast.Lambda):
            scope: dict[str, Binding] = {}
            param_bids: list[int] = []
            for p in node.params:
                pb = self._alloc(p, "param")
                param_bids.append(pb.bid)
                scope[p] = pb
            self.scopes.append(scope)
            try:
                body_refs = self.visit(node.body)
            finally:
                self.scopes.pop()
            free = tuple(sorted(body_refs - set(param_bids)))
            self.lambdas[id(node)] = LambdaInfo(
                free=free,
                param_bids=tuple(param_bids),
                node=node,
                ver_tag=node.ver,
            )
            # 对本 lambda 而言是自由变量，向外继续传播
            return set(free)
        if isinstance(node, ast.Call):
            refs = self.visit(node.func)
            for a in node.args:
                refs |= self.visit(a)
            return refs
        if isinstance(node, ast.If0):
            return (
                self.visit(node.cond)
                | self.visit(node.then_e)
                | self.visit(node.else_e)
            )
        if isinstance(node, ast.Set):
            b = self._find(node.name, node.loc)
            self.targets[id(node)] = b.bid
            self._mark_mutable(b, node.loc)
            return {b.bid} | self.visit(node.value)
        if isinstance(node, ast.LetRec):
            # 同时绑定：先一次性建立全部槽位（ID 按声明顺序连续分配）
            group: dict[str, Binding] = {}
            rec_bids: list[int] = []
            for rb in node.bindings:
                gb = self._alloc(rb.name, "rec")
                group[rb.name] = gb
                rec_bids.append(gb.bid)
            self.rec_groups[id(node)] = list(zip(
                [rb.name for rb in node.bindings], rec_bids
            ))
            self.scopes.append(group)
            try:
                refs: set[int] = set()
                # 各 lambda 互相可见；遍历顺序不影响结果
                for rb in node.bindings:
                    refs |= self.visit(rb.func)
                    if node.ver is not None:
                        info = self.lambdas[id(rb.func)]
                        self.lambdas[id(rb.func)] = LambdaInfo(
                            free=info.free,
                            param_bids=info.param_bids,
                            node=info.node,
                            ver_tag=info.ver_tag,
                            group=node.ver,
                            member=rb.name,
                            group_bids=tuple(rec_bids),
                        )
                refs |= self.visit(node.body)
            finally:
                self.scopes.pop()
            return refs - set(rec_bids)
        raise ScopeError(f"unexpected AST node during resolution: {type(node).__name__}")


def resolve(root: ast.Expr) -> Resolved:
    r = _Resolver()
    r.visit(root)
    return Resolved(
        bindings=r.bindings,
        lambdas=r.lambdas,
        targets=r.targets,
        let_bids=r.let_bids,
        rec_groups=r.rec_groups,
        next_id=r._next,
    )

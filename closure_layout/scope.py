"""词法作用域分析：绑定 ID、引用解析、自由变量、装箱判定。

绑定 ID（bid）按语法遍历顺序逐一分配，与名字无关：同名遮蔽产生不同
bid，自由变量集合一律按 bid 升序输出，绝不按名字混淆。

每个绑定记录其“宿主函数区域” ``host_fn``：

- 形参属于声明它的 lambda；
- let 绑定属于文本上包含该 let 的函数区域；
- letrec 组成员绑定属于建立该组帧的函数区域（letrec 的宿主）。

自由变量按后序吸收计算：

1. 直接使用：引用处所在函数区域 != 绑定宿主（成员函数对本组函数的
   引用走环境槽 0 的共享组帧，不算自由变量）；
2. 嵌套 lambda 的自由变量在其转换完成后吸收进外层函数：其中宿主就
   是外层函数的绑定由外层局部提供，其余成为外层自由变量；
3. letrec 各成员函数的自由变量吸收进组宿主函数（宿主局部能提供的
   以局部帧/cell 提供，其余继续向外传播）。

装箱：形参/let 绑定仅当“被 set 赋值”且“出现在任一函数最终自由变量
集合中”时才装入共享 cell；组绑定由组帧天然共享，不装箱。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast
from .errors import NodeLimitError
from .ast import MAX_NODES, count_nodes

PARAM = "param"
LET = "let"
GROUP = "group"


@dataclass(frozen=True, slots=True)
class BindInfo:
    bid: int
    name: str
    kind: str  # PARAM / LET / GROUP
    host_fn: int               # 宿主函数区域（main 为 -1）
    boxed: bool = False
    gid: int | None = None
    group_slot: int | None = None


@dataclass(frozen=True, slots=True)
class LambdaInfo:
    node_id: int
    fid: str
    name: str
    param_bids: tuple[int, ...]
    free: tuple[int, ...]         # bid 升序
    group_gid: int | None         # 非 None 即 letrec 成员
    boxed_params: tuple[int, ...]
    is_member: bool


@dataclass(frozen=True, slots=True)
class GroupInfo:
    gid: int
    node_id: int
    host_fn: int
    bids: tuple[int, ...]
    slot_of: dict[int, int]
    member_fids: tuple[str, ...]
    member_lambda_ids: tuple[int, ...]
    size: int


@dataclass(frozen=True, slots=True)
class ScopeInfo:
    bid_info: dict[int, BindInfo]
    lambda_info: dict[int, LambdaInfo]
    group_info: dict[int, GroupInfo]
    ref_target: dict[int, int]
    fid_of_lambda: dict[int, str]
    lambda_fids_in_order: tuple[str, ...]
    lambda_node_by_id: dict[int, ast.Lambda]
    gid_of_letrec: dict[int, int]
    let_bid: dict[int, int]
    set_tags: dict[int, int]


@dataclass(slots=True)
class _Frame:
    names: dict[str, int]
    kind: str             # 'params' | 'let' | 'group'
    owner_fn: int
    gid: int | None = None


class _Analyzer:
    def __init__(self, root: ast.Node) -> None:
        self.root = root
        self.bid_info: dict[int, BindInfo] = {}
        self.frames: list[_Frame] = []
        self.ref_target: dict[int, int] = {}
        self.ref_fn: dict[int, int] = {}
        self.assigned: set[int] = set()
        self.set_tags: dict[int, int] = {}
        self.param_bids: dict[int, tuple[int, ...]] = {}
        self.lambda_name: dict[int, str] = {}
        self.member_gid: dict[int, int] = {}
        self.lambda_nodes: dict[int, ast.Lambda] = {}
        self.node_by_fn: dict[int, ast.Lambda] = {}
        self.fn_order: list[int] = []
        self.groups: dict[int, GroupInfo] = {}
        self.gid_of_letrec: dict[int, int] = {}
        self.let_bid: dict[int, int] = {}
        self.member_of_fn: dict[int, int] = {}      # fn -> gid
        self.fn_of_lambda_node: dict[int, int] = {}  # lambda 节点 id -> fn
        self.letrec_of_node: dict[int, tuple[int, list[int]]] = {}
        self.next_bid = 0
        self.next_gid = 0
        self.next_fn = 0
        self._set_seq = 0

    # --------------------------------------------------------------- 工具
    def _alloc_bid(self, name: str, kind: str, host_fn: int,
                   gid: int | None, group_slot: int | None) -> int:
        bid = self.next_bid
        self.next_bid += 1
        self.bid_info[bid] = BindInfo(
            bid=bid, name=name, kind=kind, host_fn=host_fn,
            boxed=False, gid=gid, group_slot=group_slot)
        return bid

    def _push(self, names: dict[str, int], kind: str, owner_fn: int,
              gid: int | None = None) -> None:
        self.frames.append(_Frame(names, kind, owner_fn, gid))

    def _resolve(self, name: str, node: ast.Node) -> int:
        from .errors import UnboundVariableError
        for fr in reversed(self.frames):
            if name in fr.names:
                return fr.names[name]
        pos = node.pos
        raise UnboundVariableError(
            f"未绑定的变量 {name!r}",
            pos[0] if pos else None, pos[1] if pos else None)

    def _new_fn(self, node: ast.Lambda, name: str) -> int:
        fnid = self.next_fn
        self.next_fn += 1
        self.fn_order.append(fnid)
        self.lambda_nodes[id(node)] = node
        self.node_by_fn[fnid] = node
        self.lambda_name[id(node)] = name
        return fnid

    def _bind_params(self, lam: ast.Lambda, fnid: int) -> tuple[int, ...]:
        bids = tuple(self._alloc_bid(p, PARAM, fnid, None, None)
                     for p in lam.params)
        self.param_bids[id(lam)] = bids
        self._push(dict(zip(lam.params, bids)), "params", fnid)
        return bids

    # ----------------------------------------------------- 第一遍：结构
    def analyze(self) -> ScopeInfo:
        self._walk(self.root, -1)

        # 直接使用集合。
        direct: dict[int, set[int]] = {-1: set()}
        for fnid in self.fn_order:
            direct[fnid] = set()
        for node_id, bid in self.ref_target.items():
            fnid = self.ref_fn[node_id]
            own_gid = self.member_of_fn.get(fnid)
            bi = self.bid_info[bid]
            if bi.host_fn == fnid:
                continue
            if bi.kind == GROUP and own_gid is not None and bi.gid == own_gid:
                continue  # 本组函数经组帧访问
            direct[fnid].add(bid)

        free: dict[int, set[int]] = {fn: set(s)
                                     for fn, s in direct.items()}

        def absorb(dst: int, src: set[int]) -> None:
            own_gid = self.member_of_fn.get(dst)
            for bid in src:
                bi = self.bid_info[bid]
                if bi.host_fn == dst:
                    continue  # 外层局部可提供
                if bi.kind == GROUP and own_gid is not None \
                        and bi.gid == own_gid:
                    continue  # 本组帧经环境槽 0 取
                free[dst].add(bid)

        # 后序折叠：沿 AST 递归处理函数区域嵌套。
        self._fold(self.root, -1, free, absorb)

        # 装箱判定：被赋值且经环境捕获（出现在某个函数自由集合中）。
        captured: set[int] = set()
        for s in free.values():
            captured |= s
        for bid, info in self.bid_info.items():
            if (info.kind in (PARAM, LET) and bid in self.assigned
                    and bid in captured):
                self.bid_info[bid] = BindInfo(
                    bid=bid, name=info.name, kind=info.kind,
                    host_fn=info.host_fn, boxed=True,
                    gid=info.gid, group_slot=info.group_slot)

        lambda_info: dict[int, LambdaInfo] = {}
        fid_of_lambda: dict[int, str] = {}
        fids_in_order: list[str] = []
        for fnid in self.fn_order:
            node = self.node_by_fn[fnid]
            fid = f"f{fnid}"
            fids_in_order.append(fid)
            fid_of_lambda[id(node)] = fid
            gid = self.member_gid.get(id(node))
            pb = self.param_bids[id(node)]
            boxed = tuple(i for i, b in enumerate(pb)
                          if self.bid_info[b].boxed)
            lambda_info[id(node)] = LambdaInfo(
                node_id=id(node), fid=fid,
                name=self.lambda_name[id(node)],
                param_bids=pb, free=tuple(sorted(free[fnid])),
                group_gid=gid, boxed_params=boxed,
                is_member=gid is not None)

        return ScopeInfo(
            bid_info=self.bid_info,
            lambda_info=lambda_info,
            group_info=self.groups,
            ref_target=self.ref_target,
            fid_of_lambda=fid_of_lambda,
            lambda_fids_in_order=tuple(fids_in_order),
            lambda_node_by_id=self.lambda_nodes,
            gid_of_letrec=self.gid_of_letrec,
            let_bid=self.let_bid,
            set_tags=self.set_tags)

    def _fold(self, n: ast.Node, fnid: int, free: dict, absorb) -> None:
        """后序：先完成子函数区域，再把其自由集合吸收进当前区域。"""
        if isinstance(n, (ast.Int, ast.Var)):
            return
        if isinstance(n, ast.Add):
            self._fold(n.lhs, fnid, free, absorb)
            self._fold(n.rhs, fnid, free, absorb)
        elif isinstance(n, ast.Set):
            self._fold(n.value, fnid, free, absorb)
        elif isinstance(n, ast.If0):
            self._fold(n.cond, fnid, free, absorb)
            self._fold(n.then, fnid, free, absorb)
            self._fold(n.els, fnid, free, absorb)
        elif isinstance(n, ast.Call):
            self._fold(n.func, fnid, free, absorb)
            for a in n.args:
                self._fold(a, fnid, free, absorb)
        elif isinstance(n, ast.Let):
            self._fold(n.bound, fnid, free, absorb)
            self._fold(n.body, fnid, free, absorb)
        elif isinstance(n, ast.Lambda):
            child = self.fn_of_lambda_node[id(n)]
            self._fold(n.body, child, free, absorb)
            absorb(fnid, free[child])
        elif isinstance(n, ast.Letrec):
            host, members = self.letrec_of_node[id(n)]
            for _name, lam in n.bindings:
                child = self.fn_of_lambda_node[id(lam)]
                self._fold(lam.body, child, free, absorb)
            for child in members:
                absorb(host, free[child])
            self._fold(n.body, host, free, absorb)

    # ------------------------------------------------------------- 遍历
    def _walk(self, n: ast.Node, fnid: int) -> None:
        if isinstance(n, ast.Int):
            return
        if isinstance(n, ast.Var):
            self._use(n, n.name, fnid)
        elif isinstance(n, ast.Add):
            self._walk(n.lhs, fnid)
            self._walk(n.rhs, fnid)
        elif isinstance(n, ast.Set):
            self._use(n, n.name, fnid, assign=True)
            self._walk(n.value, fnid)
        elif isinstance(n, ast.If0):
            self._walk(n.cond, fnid)
            self._walk(n.then, fnid)
            self._walk(n.els, fnid)
        elif isinstance(n, ast.Call):
            self._walk(n.func, fnid)
            for a in n.args:
                self._walk(a, fnid)
        elif isinstance(n, ast.Let):
            self._walk(n.bound, fnid)
            bid = self._alloc_bid(n.name, LET, fnid, None, None)
            self.let_bid[id(n)] = bid
            self._push({n.name: bid}, "let", fnid)
            self._walk(n.body, fnid)
            self.frames.pop()
        elif isinstance(n, ast.Lambda):
            child = self._new_fn(n, "lambda")
            self.fn_of_lambda_node[id(n)] = child
            self._bind_params(n, child)
            self._walk(n.body, child)
            self.frames.pop()
        elif isinstance(n, ast.Letrec):
            self._walk_letrec(n, fnid)
        else:  # pragma: no cover
            raise AssertionError(f"未处理节点 {type(n).__name__}")

    def _use(self, node: ast.Node, name: str, fnid: int,
             assign: bool = False) -> int:
        bid = self._resolve(name, node)
        self.ref_target[id(node)] = bid
        self.ref_fn[id(node)] = fnid
        if assign:
            self.assigned.add(bid)
            if id(node) not in self.set_tags:
                self.set_tags[id(node)] = self._set_seq
                self._set_seq += 1
        return bid

    def _walk_letrec(self, n: ast.Letrec, fnid: int) -> None:
        gid = self.next_gid
        self.next_gid += 1
        names_dict: dict[str, int] = {}
        bids: list[int] = []
        # 组绑定的宿主是建立组帧的函数区域 fnid。
        for slot, (name, _lam) in enumerate(n.bindings):
            bid = self._alloc_bid(name, GROUP, fnid, gid, slot)
            names_dict[name] = bid
            bids.append(bid)
        self._push(names_dict, "group", fnid, gid)

        member_fns: list[int] = []
        member_lambda_ids: list[int] = []
        member_fids: list[str] = []
        for name, lam in n.bindings:
            child = self._new_fn(lam, name)
            self.fn_of_lambda_node[id(lam)] = child
            self.member_gid[id(lam)] = gid
            self.member_of_fn[child] = gid
            self._bind_params(lam, child)
            self._walk(lam.body, child)
            self.frames.pop()
            member_fns.append(child)
            member_lambda_ids.append(id(lam))
            member_fids.append(f"f{child}")

        self.groups[id(n)] = GroupInfo(
            gid=gid, node_id=id(n), host_fn=fnid, bids=tuple(bids),
            slot_of={b: i for i, b in enumerate(bids)},
            member_fids=tuple(member_fids),
            member_lambda_ids=tuple(member_lambda_ids),
            size=len(bids))
        self.gid_of_letrec[id(n)] = gid
        self.letrec_of_node[id(n)] = (fnid, member_fns)

        self._walk(n.body, fnid)
        self.frames.pop()


def analyze(root: ast.Node) -> ScopeInfo:
    """结构校验、节点上限检查与作用域分析。"""
    ast.validate(root)
    n_nodes = count_nodes(root)
    if n_nodes > MAX_NODES:
        pos = root.pos
        raise NodeLimitError(
            f"AST 节点数 {n_nodes} 超过上限 {MAX_NODES}",
            pos[0] if pos else None, pos[1] if pos else None)
    return _Analyzer(root).analyze()

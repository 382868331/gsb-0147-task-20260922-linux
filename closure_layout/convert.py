"""环境布局转换核心：AST -> 无自由变量函数表 + 显式环境 IR。

布局规则（与 :mod:`closure_layout.scope` 的分析结论对应）：

1. 每个 lambda 变成函数表一项；自由变量按绑定 ID 升序放入
   ``free_vars``，闭包构造点（``TClosure``）显式给出等长环境向量。
2. letrec 组展开为 ``TMakeGroup`` 共享组帧：成员闭包环境槽 0 指向同一
   组帧，成员体内经 ``TGetGroup`` / ``TSetGroup`` 互访；宿主与外部
   捕获者把组帧作为普通环境值传递（``TFrameGet`` / ``TFrameSet``）。
3. 被 set 且被捕获的形参/let 绑定在绑定点装入 cell（形参在函数入口
   装箱到新局部槽），环境槽传递 cell 本身（``TCellGet/TCellSet``）。
4. 调用约定统一：实参永远传原始值，装箱纯属函数内部布局，因此动态
   调用也不会出现装箱约定错配。
"""

from __future__ import annotations

from . import ast
from . import ir
from .scope import analyze, GROUP, ScopeInfo, LambdaInfo

MAIN_FID = "main"


class _Ctx:
    __slots__ = ("info", "fi", "locals", "boxed_local", "frame_slots",
                 "next_local", "own_gid", "env_offset")

    def __init__(self, info: ScopeInfo, fi: LambdaInfo | None,
                 base_locals: dict[int, int], boxed: set[int],
                 next_local: int, own_gid: int | None,
                 env_offset: int = 0) -> None:
        self.info = info
        self.fi = fi
        # bid -> 局部槽下标
        self.locals: dict[int, int] = dict(base_locals)
        self.boxed_local: set[int] = set(boxed)
        # gid -> 本函数可见组帧的局部槽
        self.frame_slots: dict[int, int] = {}
        self.next_local = next_local
        self.own_gid = own_gid
        # 成员函数闭包环境为 [组帧, outer_free...]，自由变量槽整体 +1。
        self.env_offset = env_offset


class Converter:
    def __init__(self, root: ast.Node) -> None:
        self.root = root
        self.info = analyze(root)
        self.functions: dict[str, ir.Function] = {}
        self.group_by_gid = {g.gid: g for g in self.info.group_info.values()}

    # ------------------------------------------------------------- 查询
    def _free_slot(self, ctx: _Ctx, bid: int) -> int:
        assert ctx.fi is not None
        return ctx.fi.free.index(bid) + ctx.env_offset

    def _classify(self, ctx: _Ctx, bid: int) -> tuple[str, object]:
        """返回 (种类, 数据)。

        种类：local / own-group / frame-local / frame-free / free
        """
        if bid in ctx.locals:
            return "local", ctx.locals[bid]
        bi = self.info.bid_info[bid]
        if bi.kind == GROUP:
            assert bi.gid is not None
            if ctx.own_gid is not None and bi.gid == ctx.own_gid:
                return "own-group", bi
            if bi.gid in ctx.frame_slots:
                return "frame-local", self.group_by_gid[bi.gid]
            slot = self._free_slot(ctx, bid)
            return "frame-free", (slot, bi)
        slot = self._free_slot(ctx, bid)
        return "free", slot

    # ------------------------------------------------------------- 读写
    def _read(self, ctx: _Ctx, bid: int) -> ir.TNode:
        kind, data = self._classify(ctx, bid)
        bi = self.info.bid_info[bid]
        if kind == "local":
            idx = data
            base = ir.TLocal(idx, bid)
            return ir.TCellGet(base) if bid in ctx.boxed_local else base
        if kind == "own-group":
            return ir.TGetGroup(bi.group_slot, bid)  # type: ignore[union-attr]
        if kind == "frame-local":
            grp = data
            return ir.TFrameGet(
                ir.TLocal(ctx.frame_slots[grp.gid], -1),
                bi.group_slot, bid)  # type: ignore[union-attr]
        if kind == "frame-free":
            slot, _ = data
            return ir.TFrameGet(ir.TGet(slot, bid), bi.group_slot, bid)  # type: ignore[union-attr]
        # free（PARAM/LET）
        base = ir.TGet(data, bid)
        return ir.TCellGet(base) if bi.boxed else base

    def _write(self, ctx: _Ctx, bid: int, value: ir.TNode,
               tag: int) -> ir.TNode:
        kind, data = self._classify(ctx, bid)
        bi = self.info.bid_info[bid]
        if kind == "local":
            idx = data
            if bid in ctx.boxed_local:
                return ir.TCellSet(ir.TLocal(idx, bid), value, tag)
            return ir.TSetLocal(idx, bid, value, tag)
        if kind == "own-group":
            return ir.TSetGroup(bi.group_slot, bid, value, tag)  # type: ignore[union-attr]
        if kind == "frame-local":
            grp = data
            return ir.TFrameSet(
                ir.TLocal(ctx.frame_slots[grp.gid], -1),
                bi.group_slot, bid, value, tag)  # type: ignore[union-attr]
        if kind == "frame-free":
            slot, _ = data
            return ir.TFrameSet(ir.TGet(slot, bid), bi.group_slot, bid,  # type: ignore[union-attr]
                                value, tag)
        if bi.boxed:
            return ir.TCellSet(ir.TGet(data, bid), value, tag)
        return ir.TSetFree(data, bid, value, tag)

    def _capture(self, ctx: _Ctx, bid: int) -> ir.TNode:
        """构造环境槽表达式：取槽内原始值（cell/frame 不拆包）。"""
        kind, data = self._classify(ctx, bid)
        if kind == "local":
            return ir.TLocal(data, bid)
        if kind == "own-group":
            # 同组帧固定位于成员闭包环境槽 0；作为一等值传给嵌套闭包。
            return ir.TGet(0, bid)
        if kind == "frame-local":
            grp = data
            return ir.TLocal(ctx.frame_slots[grp.gid], -1)
        if kind in ("free", "frame-free"):
            slot = data if kind == "free" else data[0]
            return ir.TGet(slot, bid)
        raise AssertionError(f"内部错误：未知捕获种类 {kind}")  # pragma: no cover

    # ------------------------------------------------------------- 作用域
    def _snapshot(self, ctx: _Ctx):
        return (dict(ctx.locals), ctx.next_local,
                dict(ctx.frame_slots), set(ctx.boxed_local))

    def _restore(self, ctx: _Ctx, snap) -> None:
        loc, nxt, frames, boxed = snap
        ctx.locals = loc
        ctx.next_local = nxt
        ctx.frame_slots = frames
        ctx.boxed_local = boxed

    def _subconv(self, n: ast.Node, ctx: _Ctx) -> ir.TNode:
        """转换一个子表达式，且不让其内部 let/letrec 槽位泄漏到外部。"""
        snap = self._snapshot(ctx)
        try:
            return self._conv_impl(n, ctx)
        finally:
            self._restore(ctx, snap)

    # 供递归调用的统一入口（保证作用域隔离）。
    def _conv(self, n: ast.Node, ctx: _Ctx) -> ir.TNode:
        return self._subconv(n, ctx)

    # ------------------------------------------------------------- 转换
    def _conv_impl(self, n: ast.Node, ctx: _Ctx) -> ir.TNode:
        if isinstance(n, ast.Int):
            return ir.TInt(n.value)
        if isinstance(n, ast.Var):
            return self._read(ctx, self.info.ref_target[id(n)])
        if isinstance(n, ast.Add):
            return ir.TAdd(self._conv(n.lhs, ctx), self._conv(n.rhs, ctx))
        if isinstance(n, ast.Set):
            bid = self.info.ref_target[id(n)]
            tag = self.info.set_tags[id(n)]
            return self._write(ctx, bid, self._conv(n.value, ctx), tag)
        if isinstance(n, ast.If0):
            return ir.TIf0(self._conv(n.cond, ctx),
                           self._conv(n.then, ctx),
                           self._conv(n.els, ctx))
        if isinstance(n, ast.Call):
            return ir.TCall(
                self._conv(n.func, ctx),
                tuple(self._conv(a, ctx) for a in n.args))
        if isinstance(n, ast.Let):
            bound = self._conv(n.bound, ctx)
            bid = self.info.let_bid[id(n)]
            slot = ctx.next_local
            ctx.next_local += 1
            ctx.locals[bid] = slot
            if self.info.bid_info[bid].boxed:
                ctx.boxed_local.add(bid)
                bound = ir.TCell(bound)
            return ir.TLet(bound, self._conv(n.body, ctx), bid)
        if isinstance(n, ast.Lambda):
            li = self.info.lambda_info[id(n)]
            assert not li.is_member, "成员 lambda 只能由 letrec 展开处理"
            env = tuple(self._capture(ctx, b) for b in li.free)
            return ir.TClosure(li.fid, env)
        if isinstance(n, ast.Letrec):
            return self._conv_letrec(n, ctx)
        raise AssertionError(f"未处理节点 {type(n).__name__}")  # pragma: no cover

    def _conv_letrec(self, n: ast.Letrec, ctx: _Ctx) -> ir.TNode:
        grp = self.info.group_info[id(n)]
        frame_slot = ctx.next_local
        ctx.next_local += 1
        ctx.frame_slots[grp.gid] = frame_slot

        # 成员闭包（环境槽 0 = 组帧；其后为 outer free 捕获）。
        fills: list[tuple[int, ir.TClosure]] = []
        for slot, (_name, lam) in enumerate(n.bindings):
            li = self.info.lambda_info[id(lam)]
            env: list[ir.TNode] = [ir.TLocal(frame_slot, -1)]
            env.extend(self._capture(ctx, b) for b in li.free)
            fills.append((slot, ir.TClosure(li.fid, tuple(env))))

        body = self._conv(n.body, ctx)
        for slot, clo in reversed(fills):
            body = ir.TGroupFill(frame_slot, slot, clo, body)
        return ir.TLet(ir.TMakeGroup(grp.size), body, -1)

    # ------------------------------------------------------------- 入口
    def convert(self) -> ir.Program:
        # 先为每个 lambda 生成函数体（顺序无关，fid 已由分析确定）。
        for li in self.info.lambda_info.values():
            lam = self.info.lambda_node_by_id[li.node_id]
            self._build_function(lam, li)

        main_ctx = _Ctx(self.info, None, {}, set(), 0, None)
        main_body = self._conv(self.root, main_ctx)
        self.functions[MAIN_FID] = ir.Function(
            fid=MAIN_FID, name="<main>", nparams=0,
            free_vars=(), outer_free=(), group_size=None,
            boxed_params=(), body=main_body)

        binding_info = {
            bid: (info.name, info.boxed)
            for bid, info in self.info.bid_info.items()}
        return ir.Program(
            functions=self.functions, main_fid=MAIN_FID,
            binding_info=binding_info)

    def _build_function(self, lam: ast.Lambda, li: LambdaInfo) -> None:
        nparams = len(li.param_bids)
        offset = 1 if li.is_member else 0
        ctx = _Ctx(self.info, li,
                   {b: i for i, b in enumerate(li.param_bids)},
                   set(), nparams, li.group_gid, env_offset=offset)

        # 先建立装箱形参的新局部槽映射，再转换函数体；运行时最外层
        # TLet 最先压入，故按下标升序分配槽位。
        boxed_list = list(li.boxed_params)
        for r, pi in enumerate(boxed_list):
            bid = li.param_bids[pi]
            ctx.locals[bid] = nparams + r
            ctx.boxed_local.add(bid)
        ctx.next_local = nparams + len(boxed_list)

        body = self._conv(lam.body, ctx)
        for pi in reversed(boxed_list):
            bid = li.param_bids[pi]
            body = ir.TLet(ir.TCell(ir.TLocal(pi, bid)), body, bid)

        self.functions[li.fid] = ir.Function(
            fid=li.fid, name=li.name, nparams=nparams,
            free_vars=li.free, outer_free=li.free,
            group_size=(self.group_by_gid[li.group_gid].size
                        if li.group_gid is not None else None),
            boxed_params=li.boxed_params, body=body)


def convert(root: ast.Node) -> ir.Program:
    """把源 AST 转换为 :class:`closure_layout.ir.Program`。

    抛出： :class:`ValidationError` / :class:`NodeLimitError` /
    :class:`UnboundVariableError` 等（见 :mod:`closure_layout.errors`）。
    """
    return Converter(root).convert()

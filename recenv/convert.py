"""核心转换：闭包转换 + 显式环境布局。

转换后的函数体无自由变量：所有外部引用变成对环境记录槽位 ``(depth, slot)``
的显式访问。槽位顺序规则：

* 函数捕获槽：自由绑定 ID 升序（与名字无关，遮蔽不混淆）；
* 其后按参数声明顺序放置参数槽；
* let / letrec 帧：一绑定一槽，letrec 组每槽一个未填充 Cell。

可变绑定（被 ``set`` 的 let 绑定 / 被 set 的参数）槽内放共享 Cell；
闭包抽取环境时 Cell 原样传递，故多个闭包共享同一可变单元。
letrec 组的各成员闭包共享同一个组帧记录，从而互递归。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import ASTError
from .resolver import Binding, LambdaInfo, resolve

MAIN_ID = "<main>"


@dataclass
class _Frame:
    """转换器视角的词法帧：绑定 ID -> 槽位。"""

    slots: dict[int, int]
    parent: "_Frame | None"
    n_slots: int

    def locate(self, bid: int) -> tuple[int, int]:
        f: _Frame | None = self
        depth = 0
        while f is not None:
            if bid in f.slots:
                return depth, f.slots[bid]
            f = f.parent
            depth += 1
        raise ASTError(f"internal error: binding id {bid} has no runtime slot")


class _Converter:
    def __init__(self, root: ast.Expr) -> None:
        self.root = root
        self.resolved = resolve(root)
        self.functions: dict[str, ast.TFunction] = {}
        self.layout: dict[str, list[str]] = {}
        self._fid_counter = 0

    # -- 辅助 --------------------------------------------------------------

    def _binding(self, bid: int) -> Binding:
        return self.resolved.bindings[bid]

    def _locate_ref(self, frame: _Frame | None, bid: int) -> tuple[int, int]:
        if frame is None:
            raise ASTError(f"internal error: ref to {bid} outside every frame")
        return frame.locate(bid)

    def _read(self, frame: _Frame | None, bid: int) -> ast.TExpr:
        depth, slot = self._locate_ref(frame, bid)
        b = self._binding(bid)
        # 可变绑定与 letrec 函数槽都装在 Cell 里，读取必须解引用
        if b.mutable or b.kind == "rec":
            return ast.TCellGet(depth, slot, b.name)
        return ast.TGet(depth, slot)

    def _new_fid(self) -> str:
        fid = f"f{self._fid_counter}"
        self._fid_counter += 1
        return fid

    # -- 表达式转换 --------------------------------------------------------

    def conv(
        self, node: ast.Expr, frame: _Frame | None, tail: bool = False
    ) -> ast.TExpr:
        """把源表达式转换为 IR。

        tail=True 表示该表达式处于函数体（或主表达式）的尾位置：
        其返回值直接成为外层结果、外层调用现场之后不再执行任何动作。
        """
        if isinstance(node, ast.Int):
            return ast.TConst(node.value)
        if isinstance(node, ast.Var):
            bid = self.resolved.targets[id(node)]
            return self._read(frame, bid)
        if isinstance(node, ast.Add):
            return ast.TPrim(
                "add",
                # 加法还要对结果归约，实参调用不是尾位置
                (self.conv(node.lhs, frame), self.conv(node.rhs, frame)),
            )
        if isinstance(node, ast.Call):
            return ast.TCall(
                self.conv(node.func, frame),
                tuple(self.conv(a, frame) for a in node.args),
                # 尾位置调用：返回值直接外抛，本帧之后不再被需要
                tail=tail,
            )
        if isinstance(node, ast.If0):
            return ast.TIf0(
                self.conv(node.cond, frame),
                # 两分支与整个 if0 处于同一尾位置（条件本身不是）
                self.conv(node.then_e, frame, tail),
                self.conv(node.else_e, frame, tail),
            )
        if isinstance(node, ast.Set):
            bid = self.resolved.targets[id(node)]
            b = self._binding(bid)
            depth, slot = self._locate_ref(frame, bid)
            return ast.TCellSet(
                # 赋值还要写 cell 并产生事件，其值表达式不是尾位置
                depth, slot, self.conv(node.value, frame), b.name
            )
        if isinstance(node, ast.Let):
            return self._conv_let(node, frame, tail)
        if isinstance(node, ast.Lambda):
            # 闭包构造只产生函数值，从不处于尾调用链上
            return self._conv_lambda(node, frame)
        if isinstance(node, ast.LetRec):
            return self._conv_letrec(node, frame, tail)
        raise ASTError(f"cannot convert node of type {type(node).__name__}")

    def _conv_let(
        self, node: ast.Let, frame: _Frame | None, tail: bool
    ) -> ast.TExpr:
        bid = self.resolved.let_bids[id(node)]
        b = self._binding(bid)
        tvalue = self.conv(node.value, frame)
        init: ast.TExpr = ast.TMkCell(tvalue) if b.mutable else tvalue
        inner = _Frame({bid: 0}, frame, 1)
        # let 的尾位置由其 body 继承；被闭包捕获的绑定随帧记录存活，
        # 尾调用只丢弃调用者激活帧，不影响这条新追加的词法帧
        tbody = self.conv(node.body, inner, tail)
        label = f"{'cell' if b.mutable else 'val'}:{b.name}#id{bid}"
        return ast.TWithFrame((init,), tbody, (label,))

    def _conv_lambda(self, node: ast.Lambda, frame: _Frame | None) -> ast.TExpr:
        info: LambdaInfo = self.resolved.lambdas[id(node)]
        fid = self._new_fid()

        # 捕获记录槽位：自由绑定 ID 升序
        cap_slots = {bid: i for i, bid in enumerate(info.free)}

        def _kind(b: Binding) -> str:
            return "cell" if (b.mutable or b.kind == "rec") else "val"

        labels = [
            f"{_kind(self._binding(bid))}:"
            f"{self._binding(bid).name}#id{bid}"
            for bid in info.free
        ]

        # 参数槽紧随其后；可变参数入 Cell
        param_names = list(node.params)
        mutable_params: list[bool] = []
        for i, pbid in enumerate(info.param_bids):
            pb = self._binding(pbid)
            cap_slots[pbid] = len(info.free) + i
            mutable_params.append(pb.mutable)
            labels.append(
                f"{_kind(pb)}:param {pb.name}#id{pbid}"
            )

        # 激活帧：捕获槽 + 参数槽全部在 depth 0；更外层词法帧不再出现
        # （外部引用已全部扁平进捕获记录），故父帧为 None。
        # 函数体是新的尾位置根：体内的尾调用可以复用本激活帧。
        act_frame = _Frame(cap_slots, None, len(labels))
        tbody = self.conv(node.body, act_frame, tail=True)

        # 闭包构造环境：从当前词法链原样抽取（Cell 槽抽出的仍是 Cell）
        env_inits = tuple(self._raw_read(frame, bid) for bid in info.free)

        fn = ast.TFunction(
            fid=fid,
            params=tuple(param_names),
            mutable_params=tuple(mutable_params),
            n_capture=len(info.free),
            n_slots=len(labels),
            body=tbody,
            free_names=tuple(self._binding(bid).name for bid in info.free),
        )
        self.functions[fid] = fn
        self.layout[fid] = labels
        return ast.TMkClosure(fid, env_inits)

    def _raw_read(self, frame: _Frame | None, bid: int) -> ast.TExpr:
        """抽出环境槽时一律取原始值（int / 闭包 / Cell）。"""
        depth, slot = self._locate_ref(frame, bid)
        return ast.TGet(depth, slot)

    def _conv_letrec(self, node: ast.LetRec, frame: _Frame | None, tail: bool) -> ast.TExpr:
        members = self.resolved.rec_groups[id(node)]
        # 组帧：每槽一个未填充 Cell（声明顺序 = 绑定 ID 顺序）
        group_slots = {bid: i for i, (_, bid) in enumerate(members)}
        group_frame = _Frame(group_slots, frame, len(members))

        # 在组帧作用域中转换各 lambda（互递归引用解析到本组 cell 槽）
        closures: list[ast.TExpr] = []
        for rb in node.bindings:
            closures.append(self.conv(rb.func, group_frame))

        inits = tuple(ast.TMkCell(None) for _ in members)
        labels = tuple(f"cell-rec:{name}#id{bid}" for name, bid in members)
        fills = tuple(
            ast.TFillCell(0, slot, closure, name)
            for (slot, closure), (name, _) in zip(
                enumerate(closures), members
            )
        )
        # 组体继承外层尾位置；填充表达式只是布局簿记，不是源语言尾调用
        tbody = self.conv(node.body, group_frame, tail)
        return ast.TWithFrame(inits, ast.TBegin(fills + (tbody,)), labels)

    def run(self) -> ast.Program:
        # 主表达式从非尾上下文开始：主层不是函数激活帧，顶层 Call 仍需
        # 正常建立第 1 层；尾位置（tail=True）只在函数体内部成立。
        tmain = self.conv(self.root, None, tail=False)
        self.layout[MAIN_ID] = [
            "<empty environment chain; let/letrec append frames via TWithFrame>"
        ]
        return ast.Program(
            functions=self.functions,
            main=tmain,
            layout=self.layout,
            n_nodes=ast.count_nodes(self.root),
        )


def convert(root: ast.Expr) -> ast.Program:
    """把源 AST 转换为闭包转换 + 显式环境布局的 :class:`~recenv.ast_nodes.Program`。

    转换是纯函数：不修改输入，失败时不产生任何部分输出（异常直接传播）。

    Raises:
        ASTError: 根不是合法源表达式
        NodeLimitError: 节点数超过 500
        UnboundVariableError: 存在未绑定变量（错误可定位到 Var/Set 节点 loc）
        ScopeError: 其他静态作用域错误（如对 letrec 名 set）
    """
    if not isinstance(root, ast.Node) or isinstance(root, ast.RecBinding):
        raise ASTError(
            f"convert() expects a source expression, got {type(root).__name__}"
        )
    # 节点上限：先于解析/转换检查，失败时没有任何部分产物
    ast.check_limit(root)
    return _Converter(root).run()

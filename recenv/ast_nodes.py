"""源 AST 与公开数据结构。

源语言（全部值为整数，函数是一等值）::

    expr := Int(value)                          # 整数字面量
          | Var(name)                           # 变量引用
          | Add(lhs, rhs)                       # 整数加法，先左后右
          | Let(name, value, body)              # 单态不可变绑定（被 set 时自动升级）
          | Lambda(params, body)                # 函数，params 为字符串列表
          | Call(func, args)                    # 调用，严格从左到右求值
          | If0(cond, then_e, else_e)           # cond == 0 走 then
          | Set(name, value)                    # 对词法上最近的同名绑定赋值
          | LetRec(bindings, body)              # 同时绑定的递归函数组

约定
----
* 每个节点带一个可选 ``loc`` 字符串用于错误定位。
* ``Int`` 拒绝 bool、非 int、float NaN/Infinity（通用契约）。
* 名字必须是非空字符串；lambda 参数不得重名；letrec 组内不得重名。
* 对 letrec 绑定名使用 ``set`` 是静态错误（函数槽不可重新赋值；
  函数内部的可变状态应通过外层 let 建立）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Union

from .errors import ASTError, NodeLimitError

MAX_NODES = 500

# AST 联合类型
Expr = Union[
    "Int", "Var", "Add", "Let", "Lambda", "Call", "If0", "Set", "LetRec"
]


def _check_name(name: object, what: str, loc: str | None) -> str:
    prefix = f"{loc}: " if loc else ""
    if not isinstance(name, str):
        raise ASTError(f"{prefix}{what} must be a string, got {type(name).__name__}")
    if not name:
        raise ASTError(f"{prefix}{what} must be a non-empty string")
    return name


def _check_int(value: object, loc: str | None) -> int:
    """整数参数拒绝 bool；float 仅接受有限的整值 float，拒绝 NaN/Infinity。"""
    prefix = f"{loc}: " if loc else ""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ASTError(
            f"{prefix}integer literal must be an int (bool rejected), "
            f"got {type(value).__name__}"
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ASTError(f"{prefix}integer literal must be finite, got {value!r}")
        if not value.is_integer():
            raise ASTError(
                f"{prefix}integer literal must be an integral value, got {value!r}"
            )
        return int(value)
    return value


def _check_expr(node: object, what: str, loc: str | None) -> "Expr":
    prefix = f"{loc}: " if loc else ""
    if not isinstance(node, Node):
        raise ASTError(
            f"{prefix}{what} must be an AST node, got {type(node).__name__}"
        )
    return node  # type: ignore[return-value]


def _check_param_list(params: object, loc: str | None) -> list[str]:
    prefix = f"{loc}: " if loc else ""
    if not isinstance(params, (list, tuple)):
        raise ASTError(
            f"{prefix}lambda params must be a list/tuple of names, "
            f"got {type(params).__name__}"
        )
    result: list[str] = []
    seen: set[str] = set()
    for p in params:
        name = _check_name(p, "lambda parameter", loc)
        if name in seen:
            raise ASTError(f"{prefix}duplicate lambda parameter: {name!r}")
        seen.add(name)
        result.append(name)
    return result


@dataclass(frozen=True)
class Node:
    # kw_only：子类字段可正常使用位置参数，loc 仅以 loc=... 提供
    loc: str | None = field(default=None, compare=False, kw_only=True)


@dataclass(frozen=True)
class Int(Node):
    value: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _check_int(self.value, self.loc))


@dataclass(frozen=True)
class Var(Node):
    name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _check_name(self.name, "variable name", self.loc))


@dataclass(frozen=True)
class Add(Node):
    lhs: Expr = None  # type: ignore[assignment]
    rhs: Expr = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "lhs", _check_expr(self.lhs, "add left operand", self.loc))
        object.__setattr__(self, "rhs", _check_expr(self.rhs, "add right operand", self.loc))


@dataclass(frozen=True)
class Let(Node):
    name: str = ""
    value: Expr = None  # type: ignore[assignment]
    body: Expr = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _check_name(self.name, "let binding name", self.loc))
        object.__setattr__(self, "value", _check_expr(self.value, "let value", self.loc))
        object.__setattr__(self, "body", _check_expr(self.body, "let body", self.loc))


@dataclass(frozen=True)
class Lambda(Node):
    params: tuple[str, ...] = ()
    body: Expr = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        ps = tuple(_check_param_list(self.params, self.loc))
        object.__setattr__(self, "params", ps)
        object.__setattr__(self, "body", _check_expr(self.body, "lambda body", self.loc))


@dataclass(frozen=True)
class Call(Node):
    func: Expr = None  # type: ignore[assignment]
    args: tuple[Expr, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "func", _check_expr(self.func, "call function", self.loc))
        prefix = f"{self.loc}: " if self.loc else ""
        if not isinstance(self.args, (list, tuple)):
            raise ASTError(f"{prefix}call args must be a list/tuple")
        checked = tuple(_check_expr(a, "call argument", self.loc) for a in self.args)
        object.__setattr__(self, "args", checked)


@dataclass(frozen=True)
class If0(Node):
    cond: Expr = None  # type: ignore[assignment]
    then_e: Expr = None  # type: ignore[assignment]
    else_e: Expr = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cond", _check_expr(self.cond, "if0 condition", self.loc))
        object.__setattr__(self, "then_e", _check_expr(self.then_e, "if0 then", self.loc))
        object.__setattr__(self, "else_e", _check_expr(self.else_e, "if0 else", self.loc))


@dataclass(frozen=True)
class Set(Node):
    name: str = ""
    value: Expr = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _check_name(self.name, "set target", self.loc))
        object.__setattr__(self, "value", _check_expr(self.value, "set value", self.loc))


@dataclass(frozen=True)
class RecBinding(Node):
    """letrec 中的单个函数绑定：名字 + lambda。"""

    name: str = ""
    func: Lambda = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "name", _check_name(self.name, "letrec binding name", self.loc)
        )
        prefix = f"{self.loc}: " if self.loc else ""
        if not isinstance(self.func, Lambda):
            raise ASTError(
                f"{prefix}letrec binding {self.name!r} must be a lambda literal, "
                f"got {type(self.func).__name__}"
            )


@dataclass(frozen=True)
class LetRec(Node):
    bindings: tuple[RecBinding, ...] = ()
    body: Expr = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        prefix = f"{self.loc}: " if self.loc else ""
        if not isinstance(self.bindings, (list, tuple)):
            raise ASTError(f"{prefix}letrec bindings must be a list/tuple")
        if not self.bindings:
            raise ASTError(f"{prefix}letrec requires at least one binding")
        seen: set[str] = set()
        bs: list[RecBinding] = []
        for b in self.bindings:
            if not isinstance(b, RecBinding):
                raise ASTError(
                    f"{prefix}letrec binding must be RecBinding, got {type(b).__name__}"
                )
            if b.name in seen:
                raise ASTError(
                    f"{prefix}duplicate letrec binding in the same group: {b.name!r}"
                )
            seen.add(b.name)
            bs.append(b)
        object.__setattr__(self, "bindings", tuple(bs))
        object.__setattr__(self, "body", _check_expr(self.body, "letrec body", self.loc))


# ---------------------------------------------------------------------------
# 目标 IR：闭包转换 + 显式环境布局后的程序
#
# 运行期环境是帧记录的*链*：函数激活时建立一个记录（槽位 = 排序后的自由变量，
# 另加可变参数 cell），let/letrec 通过 TWithFrame 在链顶追加记录。
# (depth, slot) 中 depth=0 为当前帧，向外递增。槽内普通绑定放原值，
# 可变绑定放共享 Cell；闭包只把自己的自由变量（按绑定 ID 排序）抽到一条
# 新记录里，cell 槽原样抽取 → 多个闭包持有同一个 Cell。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TConst:
    value: int


@dataclass(frozen=True)
class TGet:
    """原始读取环境槽（int / 闭包 / Cell 都可能）。"""

    depth: int
    slot: int


@dataclass(frozen=True)
class TCellGet:
    """槽中必须是 Cell，读取其当前值。"""

    depth: int
    slot: int
    name: str  # 绑定名，仅用于调试/副作用事件


@dataclass(frozen=True)
class TCellSet:
    """写槽中 Cell；求值为写入的值。loc 为源 Set 节点的位置标签。"""

    depth: int
    slot: int
    value: "TExpr"
    name: str
    loc: str | None = field(default=None, compare=False, kw_only=True)


@dataclass(frozen=True)
class TPrim:
    """原语算术，目前只有 "add"，参数严格按序求值。

    loc 为源 Add 节点的位置标签，运行期操作数类型错误据此定位
    （与源解释器的异常位置一致）。
    """

    op: str
    args: tuple["TExpr", ...]
    loc: str | None = field(default=None, compare=False, kw_only=True)


@dataclass(frozen=True)
class TMkClosure:
    """构造闭包：引用函数表中的函数，附显式环境记录（长度 = 该函数自由变量数）。"""

    fid: str
    env: tuple["TExpr", ...]


@dataclass(frozen=True)
class TMkCell:
    """分配共享可变单元；value 为 None 表示未填充单元（letrec 组槽）。"""

    value: "TExpr | None"


@dataclass(frozen=True)
class TFillCell:
    """一次性填充未填充单元；求值为 0。对已填充单元执行是运行期错误。"""

    depth: int
    slot: int
    value: "TExpr"
    name: str


@dataclass(frozen=True)
class TCall:
    """非尾位置调用：调用方帧必须保留，实参从左到右求值。

    loc 为源 Call 节点的位置标签（调用非函数/元数不符时据此定位）。
    """

    func: "TExpr"
    args: tuple["TExpr", ...]
    loc: str | None = field(default=None, compare=False, kw_only=True)


@dataclass(frozen=True)
class TTailCall:
    """尾位置调用：函数与全部实参在*当前帧*中严格从左到右求值完成后，
    调用帧被目标函数的激活帧替换（不压栈）。

    实参求值期间当前帧仍然存活；任一实参出错都不会替换帧。
    loc 为源 Call 节点的位置标签。
    """

    func: "TExpr"
    args: tuple["TExpr", ...]
    loc: str | None = field(default=None, compare=False, kw_only=True)


@dataclass(frozen=True)
class TIf0:
    cond: "TExpr"
    then_e: "TExpr"
    else_e: "TExpr"
    loc: str | None = field(default=None, compare=False, kw_only=True)


@dataclass(frozen=True)
class TWithFrame:
    """在环境链顶追加一帧：在外层链上求完全部 inits 后执行 body。

    labels 仅用于展示环境布局，每槽一个说明字符串。
    """

    inits: tuple["TExpr", ...]
    body: "TExpr"
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class TBegin:
    """顺序求值，值为最后一个表达式（letrec 填充后执行组体用）。"""

    exprs: tuple["TExpr", ...]


TExpr = Union[
    TConst,
    TGet,
    TCellGet,
    TCellSet,
    TPrim,
    TMkClosure,
    TMkCell,
    TFillCell,
    TCall,
    TTailCall,
    TIf0,
    TWithFrame,
    TBegin,
]


@dataclass(frozen=True)
class TFunction:
    """函数表条目：函数体无自由变量，外部引用全部经环境槽。

    Attributes:
        fid: 函数 ID
        params: 参数名（声明顺序）
        mutable_params: 哪些位置的参数被 set，进入时包装成 Cell
        n_capture: 闭包环境记录长度（排序后的自由变量数）
        n_slots: 激活帧总槽数（n_capture + 可变参数 cell 数）
        body: 函数体
        free_names: 与捕获槽一一对应的绑定名（按绑定 ID 排序），仅调试用
    """

    fid: str
    params: tuple[str, ...]
    mutable_params: tuple[bool, ...]
    n_capture: int
    n_slots: int
    body: TExpr
    free_names: tuple[str, ...]


@dataclass(frozen=True)
class Program:
    """转换输出。

    Attributes:
        functions: fid -> TFunction，函数体无自由变量
        main: 主表达式（在空环境链上求值）
        layout: 调试用环境布局：fid -> 每槽语义说明；主函数为 "<main>"
        n_nodes: 源 AST 节点数
    """

    functions: dict[str, TFunction]
    main: TExpr
    layout: dict[str, list[str]]
    n_nodes: int


def count_nodes(node: object) -> int:
    """统计源 AST 节点数（含 RecBinding，不含 loc 等属性）。"""
    if isinstance(node, Int):
        return 1
    if isinstance(node, Var):
        return 1
    if isinstance(node, Add):
        return 1 + count_nodes(node.lhs) + count_nodes(node.rhs)
    if isinstance(node, Let):
        return 1 + count_nodes(node.value) + count_nodes(node.body)
    if isinstance(node, Lambda):
        return 1 + count_nodes(node.body)
    if isinstance(node, Call):
        return 1 + count_nodes(node.func) + sum(count_nodes(a) for a in node.args)
    if isinstance(node, If0):
        return 1 + count_nodes(node.cond) + count_nodes(node.then_e) + count_nodes(node.else_e)
    if isinstance(node, Set):
        return 1 + count_nodes(node.value)
    if isinstance(node, RecBinding):
        return 1 + count_nodes(node.func)
    if isinstance(node, LetRec):
        return 1 + sum(count_nodes(b) for b in node.bindings) + count_nodes(node.body)
    return 0


def check_limit(root: Expr, limit: int = MAX_NODES) -> int:
    n = count_nodes(root)
    if n > limit:
        raise NodeLimitError(n, limit, getattr(root, "loc", None))
    return n

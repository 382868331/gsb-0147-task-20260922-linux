"""转换后的目标中间表示（无自由变量函数表 + 显式环境）。

目标语言要点：

- 函数表中每个函数的自由变量已全部显式列入 ``free_vars``（按绑定 ID
  升序），函数体不再含任何词法自由变量；
- ``TClosure`` 显式给出环境向量构造表达式；
- letrec 组以 ``TMakeGroup`` 建立共享组帧；成员闭包环境槽 0 指向同一
  组帧，成员体内以 ``TGetGroup`` / ``TSetGroup`` 经环境槽 0 访问同组
  函数，从而实现互递归共享；
- 可变且被捕获的绑定显式装箱：``TCell`` / ``TCellGet`` / ``TCellSet``。

局部槽（TLocal/TSetLocal）布局：先形参，后按 TLet 出现顺序压入。
letrec 展开时，组帧本身占一个局部槽（``frame_slot``）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TNode:
    pass


@dataclass(frozen=True, slots=True)
class TInt(TNode):
    value: int


@dataclass(frozen=True, slots=True)
class TAdd(TNode):
    lhs: TNode
    rhs: TNode


@dataclass(frozen=True, slots=True)
class TClosure(TNode):
    """构造闭包：``fid`` 的函数搭配显式环境向量 ``env``。

    普通 lambda：``env`` 与函数 ``free_vars``（按绑定 ID 升序）一一对应。
    letrec 成员：``env[0]`` 是共享组帧，``env[1+k]`` 对应 ``outer_free[k]``。
    """

    fid: str
    env: tuple[TNode, ...]


@dataclass(frozen=True, slots=True)
class TCell(TNode):
    """分配共享可变 cell，初值为 ``value``。"""

    value: TNode


@dataclass(frozen=True, slots=True)
class TCellGet(TNode):
    cell: TNode


@dataclass(frozen=True, slots=True)
class TCellSet(TNode):
    """对 cell 赋值，整个表达式的值为所赋的值。"""

    cell: TNode
    value: TNode
    tag: int = -1


@dataclass(frozen=True, slots=True)
class TLocal(TNode):
    """读本函数局部槽（形参/let/组帧）。"""

    index: int
    bid: int  # 组帧槽为 -1，其余为绑定 ID


@dataclass(frozen=True, slots=True)
class TGet(TNode):
    """读当前函数闭包环境槽；``bid`` 为该槽对应的绑定 ID。"""

    slot: int
    bid: int


@dataclass(frozen=True, slots=True)
class TGetGroup(TNode):
    """经环境槽 0 取共享组帧，再读其 ``index`` 槽（letrec 同组函数）。"""

    index: int
    bid: int


@dataclass(frozen=True, slots=True)
class TSetLocal(TNode):
    index: int
    bid: int
    value: TNode
    tag: int = -1


@dataclass(frozen=True, slots=True)
class TSetFree(TNode):
    slot: int
    bid: int
    value: TNode
    tag: int = -1


@dataclass(frozen=True, slots=True)
class TSetGroup(TNode):
    index: int
    bid: int
    value: TNode
    tag: int = -1


@dataclass(frozen=True, slots=True)
class TFrameGet(TNode):
    """从任意组帧值 ``frame`` 的 ``index`` 槽读取（帧作为环境值逃逸时）。"""

    frame: TNode
    index: int
    bid: int


@dataclass(frozen=True, slots=True)
class TFrameSet(TNode):
    """向任意组帧值 ``frame`` 的 ``index`` 槽写入，值为所赋的值。"""

    frame: TNode
    index: int
    bid: int
    value: TNode
    tag: int = -1


@dataclass(frozen=True, slots=True)
class TCall(TNode):
    func: TNode
    args: tuple[TNode, ...]


@dataclass(frozen=True, slots=True)
class TIf0(TNode):
    cond: TNode
    then: TNode
    els: TNode


@dataclass(frozen=True, slots=True)
class TLet(TNode):
    """压入一个局部槽：求值 ``bound`` 后在扩展局部环境中求 ``body``。"""

    bound: TNode
    body: TNode
    bid: int  # 组帧槽为 -1


@dataclass(frozen=True, slots=True)
class TMakeGroup(TNode):
    """建立含 ``size`` 个未初始化槽的共享组帧。"""

    size: int


@dataclass(frozen=True, slots=True)
class TGroupFill(TNode):
    """把 ``value`` 写入局部 ``frame_slot`` 所指组帧的 ``slot``，再求 body。"""

    frame_slot: int
    slot: int
    value: TNode
    body: TNode


@dataclass(frozen=True, slots=True)
class Function:
    """函数表中的一项。

    - ``free_vars``：函数体子树内引用、且非本子树（本组）建立的全部
      绑定 ID，按 ID 升序；普通 lambda 即其捕获集合；
    - ``outer_free``：letrec 成员在共享组帧之外的捕获（按 ID 升序），
      普通函数等于 ``free_vars``；
    - ``group_size``：仅 letrec 成员函数非 None，给出共享组帧大小；
    - ``boxed_params``：需要以 cell 接收实参的形参下标集合。
    """

    fid: str
    name: str
    nparams: int
    free_vars: tuple[int, ...]
    outer_free: tuple[int, ...]
    group_size: int | None
    boxed_params: tuple[int, ...]
    body: TNode


@dataclass(frozen=True, slots=True)
class Program:
    """convert 的输出：无自由变量函数表 + 主表达式。"""

    functions: dict[str, Function]
    main_fid: str
    binding_info: dict[int, tuple[str, bool]]  # bid -> (名字, 是否装箱)

    @property
    def main_body(self) -> TNode:
        return self.functions[self.main_fid].body

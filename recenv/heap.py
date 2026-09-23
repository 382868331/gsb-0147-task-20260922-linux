"""显式堆：带稳定对象 ID 的 cell / closure / record。

运行态的所有共享可变状态都活在堆上，栈帧与记录槽只持有 :class:`Ref`
（对象 ID）或内联整数。序列化以 OID 为键，因此：

* 别名（多个槽指向同一 Cell）天然保留；
* 环（letrec 组的 rec-cell 指向闭包、闭包记录又指回该 cell）可表达；
* 转储顺序与对象表排列次序无关——OID 是身份，不是表位置。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import HeapLimitError

# 槽值：内联整数、对象引用，或 None（仅用于尚未填充的 cell）。
SlotVal = "int | Ref | None"

DEFAULT_HEAP_LIMIT = 100_000


@dataclass(frozen=True)
class Ref:
    """堆对象的稳定引用（对象 ID）。"""

    oid: int

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"#{self.oid}"


@dataclass
class CellObj:
    """可变单元。``filled=False`` 且 value=None 表示 letrec 未填充单元。"""

    oid: int
    value: SlotVal = None
    filled: bool = True
    kind: str = "cell"  # "cell"（普通可变单元）| "rec"（letrec 组成员槽）


@dataclass
class ClosureObj:
    """函数闭包：绑定到代码表中的函数键，捕获一条环境记录。"""

    oid: int
    fkey: str          # CodeBundle 内全局唯一函数键
    record: Ref        # 捕获记录（RecordObj）
    unit: str | None = None   # 版本化单元名（无标签函数为 None）
    version: int | None = None
    member: str | None = None  # 组成员名（独立函数为 None）


@dataclass
class RecordObj:
    """环境记录：槽位为内联整数 / 引用；parent 指向外层帧（链顶模型）。"""

    oid: int
    slots: list[SlotVal] = field(default_factory=list)
    parent: Ref | None = None
    # 该记录的语义角色："activation"（函数激活帧）、"frame"（let/letrec 追加帧）、
    # "capture"（闭包捕获记录）。
    role: str = "frame"
    # role=="activation" 时为本次激活所用函数的 fkey（迁移时用于辨认在途成员
    # 帧的 rec 槽）；其余角色为 None。
    tag: str | None = None


class Heap:
    """OID 单调分配的显式堆。OID 从 1 开始，0 保留（便于调试辨识空引用）。"""

    def __init__(self, limit: int = DEFAULT_HEAP_LIMIT) -> None:
        self._objects: dict[int, object] = {}
        self._next = 1
        self.limit = limit

    # -- 分配 --------------------------------------------------------------

    def _alloc_oid(self) -> int:
        if len(self._objects) >= self.limit:
            raise HeapLimitError(len(self._objects), self.limit)
        oid = self._next
        self._next += 1
        return oid

    def new_cell(self, value: SlotVal = None, filled: bool = True,
                 kind: str = "cell") -> Ref:
        oid = self._alloc_oid()
        self._objects[oid] = CellObj(oid=oid, value=value, filled=filled, kind=kind)
        return Ref(oid)

    def new_record(self, slots: list[SlotVal], parent: Ref | None = None,
                   role: str = "frame") -> Ref:
        oid = self._alloc_oid()
        self._objects[oid] = RecordObj(oid=oid, slots=slots, parent=parent, role=role)
        return Ref(oid)

    def new_closure(self, fkey: str, record: Ref, unit: str | None = None,
                    version: int | None = None, member: str | None = None) -> Ref:
        oid = self._alloc_oid()
        self._objects[oid] = ClosureObj(
            oid=oid, fkey=fkey, record=record,
            unit=unit, version=version, member=member,
        )
        return Ref(oid)

    # -- 访问 --------------------------------------------------------------

    def get(self, ref: Ref) -> object:
        obj = self._objects.get(ref.oid)
        if obj is None:
            raise KeyError(f"dangling heap reference #{ref.oid}")
        return obj

    def cell(self, ref: Ref) -> CellObj:
        obj = self.get(ref)
        if not isinstance(obj, CellObj):
            raise TypeError(f"#{ref.oid} is {type(obj).__name__}, expected CellObj")
        return obj

    def record(self, ref: Ref) -> RecordObj:
        obj = self.get(ref)
        if not isinstance(obj, RecordObj):
            raise TypeError(f"#{ref.oid} is {type(obj).__name__}, expected RecordObj")
        return obj

    def closure(self, ref: Ref) -> ClosureObj:
        obj = self.get(ref)
        if not isinstance(obj, ClosureObj):
            raise TypeError(f"#{ref.oid} is {type(obj).__name__}, expected ClosureObj")
        return obj

    def is_closure(self, ref: Ref) -> bool:
        return isinstance(self._objects.get(ref.oid), ClosureObj)

    # -- 批量装载（恢复用；不重新分配 OID，保持身份） ----------------------

    def load(self, oid: int, obj: object) -> None:
        if oid in self._objects:
            raise ValueError(f"heap object #{oid} loaded twice")
        self._objects[oid] = obj
        if oid >= self._next:
            self._next = oid + 1

    def unload(self, oid: int) -> None:
        """回滚用：撤销一次 load（仅迁移提交失败时调用）。"""
        self._objects.pop(oid, None)

    def __len__(self) -> int:
        return len(self._objects)

    def all_oids(self) -> list[int]:
        return sorted(self._objects)

    def reachable_from(self, roots: list[Ref]) -> set[int]:
        """从给定根出发的全部可达 OID（引用图完整性校验用，迭代不递归）。"""
        seen: set[int] = set()
        pending = [r.oid for r in roots]
        while pending:
            oid = pending.pop()
            if oid in seen:
                continue
            obj = self._objects.get(oid)
            if obj is None:
                raise KeyError(f"dangling heap reference #{oid}")
            seen.add(oid)
            if isinstance(obj, CellObj):
                if isinstance(obj.value, Ref):
                    pending.append(obj.value.oid)
            elif isinstance(obj, ClosureObj):
                pending.append(obj.record.oid)
            elif isinstance(obj, RecordObj):
                if obj.parent is not None:
                    pending.append(obj.parent.oid)
                for s in obj.slots:
                    if isinstance(s, Ref):
                        pending.append(s.oid)
        return seen

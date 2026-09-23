"""显式堆：运行期环境对象的稳定身份模型。

执行机（:mod:`recenv.machine`）不再用 Python 对象图隐式保存环境，而是把
三类对象放在显式堆上，用稳定字符串 OID 互相引用：

* ``record`` —— 环境帧记录，槽位是整数立即数或 OID 引用；
* ``cell``   —— 共享可变单元（``filled=False`` 表示 letrec 未填充单元）；
* ``closure`` —— 闭包：``(fid, record_oid, code_version)``。

帧链 = 栈帧里的 OID 列表；record/cell/closure 之间可以共享、成环
（letrec 组帧 cell 被成员闭包捕获、成员闭包又填回 cell）。
OID 在一次运行内按分配顺序单调分配（``o<序号>``），加载镜像时原样保留，
因此序列化/重排不改变任何对象身份与别名关系。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ImageFormatError, LangRuntimeError

# 未填充单元的内容哨兵（letrec 组成员槽填充前）。
UNFILLED = "\x00unfilled\x00"

OID_PREFIX = "o"


@dataclass
class CellState:
    """cell 的可变状态。value 为 int / OID(str) / None；filled 为填充标记。"""

    value: object
    filled: bool = True


class Heap:
    """记录 / 单元 / 闭包的显式容器。

    除分配与 cell 赋值外全部是只读查询；对象身份以 OID 为准，
    与 Python 对象 id 无关（后者在不同进程间不稳定，不能用于序列化）。
    """

    def __init__(self) -> None:
        self.records: dict[str, tuple[object, ...]] = {}
        self.rec_schema: dict[str, str | None] = {}
        self.cells: dict[str, CellState] = {}
        self.closures: dict[str, tuple[str, str, str]] = {}  # oid -> (fid, record_oid, version)
        self._next = 0

    # -- 分配 --------------------------------------------------------------

    def _alloc(self) -> str:
        oid = f"{OID_PREFIX}{self._next}"
        self._next += 1
        return oid

    def alloc_record(self, slots: tuple[object, ...], schema_id: str | None = None) -> str:
        oid = self._alloc()
        self.records[oid] = tuple(slots)
        self.rec_schema[oid] = schema_id
        return oid

    def alloc_cell(self, value: object = None, filled: bool = True) -> str:
        oid = self._alloc()
        self.cells[oid] = CellState(value, filled)
        return oid

    def alloc_closure(self, fid: str, record_oid: str, version: str) -> str:
        if record_oid not in self.records:
            raise LangRuntimeError("internal: closure record missing on heap")
        oid = self._alloc()
        self.closures[oid] = (fid, record_oid, version)
        return oid

    # -- 查询 / 解引用 ------------------------------------------------------

    def closure_info(self, oid: str) -> tuple[str, str, str]:
        info = self.closures.get(oid)
        if info is None:
            raise LangRuntimeError(f"internal: {oid} is not a closure")
        return info

    def cell_get(self, oid: str, name: str = "?") -> object:
        cs = self.cells.get(oid)
        if cs is None:
            raise LangRuntimeError(f"internal: {oid} is not a cell")
        if not cs.filled:
            raise LangRuntimeError(
                f"read of recursive slot {name!r} before initialization"
            )
        return cs.value

    def cell_set(self, oid: str, value: object) -> None:
        cs = self.cells.get(oid)
        if cs is None:
            raise LangRuntimeError(f"internal: {oid} is not a cell")
        cs.value = value
        cs.filled = True

    def fill_cell(self, oid: str, value: object, name: str = "?") -> None:
        cs = self.cells.get(oid)
        if cs is None or cs.filled:
            raise LangRuntimeError(
                f"recursive slot {name!r} already initialized or not a cell"
            )
        cs.value = value
        cs.filled = True

    # -- 镜像装载 -----------------------------------------------------------

    def seed_oid_counter(self, max_n: int) -> None:
        """装载后把分配计数器推到 max_n 之后，保证新分配 OID 不与镜像冲突。"""
        self._next = max(self._next, max_n + 1)

    def register_record(self, oid: str, slots: tuple[object, ...], schema_id: str | None) -> None:
        self._check_oid(oid)
        self.records[oid] = slots
        self.rec_schema[oid] = schema_id
        self._bump(oid)

    def register_cell(self, oid: str, value: object, filled: bool) -> None:
        self._check_oid(oid)
        self.cells[oid] = CellState(value, filled)
        self._bump(oid)

    def register_closure(self, oid: str, fid: str, record_oid: str, version: str) -> None:
        self._check_oid(oid)
        self.closures[oid] = (fid, record_oid, version)
        self._bump(oid)

    @staticmethod
    def _check_oid(oid: object) -> None:
        if not isinstance(oid, str) or not oid.startswith(OID_PREFIX) or len(oid) == 1:
            raise ImageFormatError(f"bad object id {oid!r}")
        if not oid[1:].isdigit():
            raise ImageFormatError(f"bad object id {oid!r}")

    def _bump(self, oid: str) -> None:
        n = int(oid[1:])
        if n >= self._next:
            self._next = n + 1

    # -- 可达性（序列化与迁移用） -------------------------------------------

    def reachable(self, roots: list[object]) -> set[str]:
        """从一批根值（int/OID 混合）求所有可达 OID。"""
        seen: set[str] = set()
        stack = [r for r in roots if isinstance(r, str) and r and r[0] == OID_PREFIX and r != UNFILLED]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            if oid in self.records:
                seen.add(oid)
                stack.extend(s for s in self.records[oid] if isinstance(s, str) and s and s[0] == OID_PREFIX)
            elif oid in self.cells:
                seen.add(oid)
                cs = self.cells[oid]
                if isinstance(cs.value, str) and cs.value and cs.value[0] == OID_PREFIX:
                    stack.append(cs.value)
            elif oid in self.closures:
                seen.add(oid)
                stack.append(self.closures[oid][1])
            else:
                raise LangRuntimeError(f"internal: dangling reference {oid}")
        return seen

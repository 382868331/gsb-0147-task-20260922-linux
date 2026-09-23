"""函数版本表：代码登记、规范化与内容哈希。

每个转换后的 :class:`~recenv.ast_nodes.TFunction` 登记为一个不可变表项
:class:`CodeEntry`，键为转换器给出的 ``fid``。带版本标签的函数另外登记到
版本表：

* 独立函数标签 ``"name@v"``（在 ``Lambda(ver=...)`` 上）→ 单元 ``name``、
  版本 ``v``，单个表项（成员名固定为 ``"$self"``）；
* 互递归组标签 ``"group@v"``（在 ``LetRec(ver=...)`` 上）→ 单元 ``group``、
  版本 ``v``，成员名 → 表项，组整体升级。

代码身份用规范化表达式树的 SHA-256 内容哈希确定：同一内容必然同哈希，
不同内容（含 set 事件所用的槽名）哈希不同。恢复时重算哈希核对，不信任
快照里声明性的“校验通过”标志。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import ASTError, CodeVersionError

SELF_MEMBER = "$self"
MAIN_FKEY = "<main>"


@dataclass(frozen=True)
class FieldSchema:
    """一个捕获槽的字段模式：名字 + 槽种类（"val" / "cell" / "rec"）。"""

    name: str
    kind: str


@dataclass(frozen=True)
class CodeEntry:
    fkey: str
    params: tuple[str, ...]
    mutable_params: tuple[bool, ...]
    body: ast.TExpr
    free_names: tuple[str, ...]
    capture_kinds: tuple[str, ...]
    unit: str | None
    version: int | None
    member: str | None          # 组成员名；独立版本函数为 "$self"；无标签为 None
    code_hash: str

    @property
    def arity(self) -> int:
        return len(self.params)

    @property
    def n_capture(self) -> int:
        return len(self.free_names)

    @property
    def n_slots(self) -> int:
        return self.n_capture + len(self.params)

    @property
    def is_versioned(self) -> bool:
        return self.unit is not None

    def external_fields(self) -> tuple[FieldSchema, ...]:
        """可迁移字段：非 rec 的捕获槽（rec 槽是同组伙伴函数，不参与迁移）。"""
        return tuple(
            FieldSchema(n, k)
            for n, k in zip(self.free_names, self.capture_kinds)
            if k != "rec"
        )


def split_tag(tag: str) -> tuple[str, int]:
    if "@" not in tag:
        raise ASTError(f"version tag must look like 'name@version', got {tag!r}")
    name, _, vs = tag.rpartition("@")
    if not name or not vs:
        raise ASTError(f"version tag must look like 'name@version', got {tag!r}")
    try:
        v = int(vs)
    except ValueError:
        raise ASTError(f"version tag version must be an integer, got {vs!r}")
    if v < 1:
        raise ASTError(f"version tag version must be >= 1, got {v}")
    return name, v


def normalize_expr(e: object) -> object:
    """把 TExpr 规范化为仅含 str/int/bool/None/list 的可 JSON 化嵌套结构。"""
    if isinstance(e, bool):
        return ["bool", e]
    if isinstance(e, int):
        return ["int", e]
    if isinstance(e, str):
        return ["str", e]
    if e is None:
        return None
    if isinstance(e, tuple):
        return ["tuple", [normalize_expr(x) for x in e]]
    if not dataclasses.is_dataclass(e):
        raise CodeVersionError(f"cannot normalize {type(e).__name__}")
    return [
        "node",
        type(e).__name__,
        [[f.name, normalize_expr(getattr(e, f.name))] for f in dataclasses.fields(e)],
    ]


def expr_hash(e: ast.TExpr) -> str:
    blob = json.dumps(
        normalize_expr(e), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _entry_from(fkey: str, fn: ast.TFunction) -> CodeEntry:
    meta_unit: str | None = None
    meta_version: int | None = None
    member: str | None = None
    if fn.group is not None:
        meta_unit, meta_version = split_tag(fn.group)
        member = fn.member
    elif fn.ver_tag is not None:
        meta_unit, meta_version = split_tag(fn.ver_tag)
        member = SELF_MEMBER
    if fn.n_capture and not fn.capture_kinds:
        raise CodeVersionError(f"function {fkey!r} missing capture_kinds metadata")
    return CodeEntry(
        fkey=fkey,
        params=tuple(fn.params),
        mutable_params=tuple(fn.mutable_params),
        body=fn.body,
        free_names=tuple(fn.free_names),
        capture_kinds=tuple(fn.capture_kinds),
        unit=meta_unit,
        version=meta_version,
        member=member,
        code_hash=expr_hash(fn.body),
    )


class CodeBundle:
    """不可变代码登记表（可合并多个 Program，从而容纳同一单元的多个版本）。"""

    def __init__(self) -> None:
        self._entries: dict[str, CodeEntry] = {}
        # (unit, version) -> {member_name: fkey}
        self._versions: dict[tuple[str, int], dict[str, str]] = {}
        self._unit_versions: dict[str, list[int]] = {}
        self.main: ast.TExpr | None = None

    @classmethod
    def from_program(cls, program: ast.Program) -> "CodeBundle":
        b = cls()
        b.add_program(program)
        return b

    def add_program(self, program: ast.Program) -> None:
        if self.main is None:
            self.main = program.main
        for fid, fn in program.functions.items():
            self.add_entry(_entry_from(fid, fn))

    def add_entry(self, entry: CodeEntry) -> CodeEntry:
        if entry.fkey in self._entries:
            existing = self._entries[entry.fkey]
            if existing.code_hash != entry.code_hash:
                raise CodeVersionError(
                    f"function key {entry.fkey!r} reused with different code"
                )
            return existing
        self._entries[entry.fkey] = entry
        if entry.is_versioned:
            self._index_version(entry)
        return entry

    def _remove_entry(self, fkey: str) -> None:
        """回滚用：撤销一次 add_entry（仅迁移提交失败时调用）。"""
        entry = self._entries.pop(fkey)
        if entry.is_versioned:
            table = self._versions.get((entry.unit, entry.version))
            if table is not None:
                table.pop(entry.member, None)
                if not table:
                    del self._versions[(entry.unit, entry.version)]
            vs = self._unit_versions.get(entry.unit)
            if vs is not None and entry.version in vs:
                vs.remove(entry.version)
                if not vs:
                    del self._unit_versions[entry.unit]

    def import_program(self, program: ast.Program, prefix: str) -> dict[str, str]:
        """把另一个 Program 的全部函数并入本表，键改为 ``prefix + 旧fid``。

        函数体中的 :class:`TMkClosure` 引用同步重写（深度复制，不触碰原
        Program）。返回 {旧 fid: 新 fkey}。新版本代码因此与在途旧代码键空间
        互不干扰，可以共存于同一张版本表。
        """
        remap = {fid: f"{prefix}{fid}" for fid in program.functions}
        for collide in remap.values():
            if collide in self._entries:
                raise CodeVersionError(
                    f"imported function key {collide!r} already exists"
                )
        for fid, fn in program.functions.items():
            new_fkey = remap[fid]
            body = _remap_closure_fids(fn.body, remap)
            tag = fn.ver_tag
            unit = version = member = None
            if fn.group is not None:
                unit, version = split_tag(fn.group)
                member = fn.member
            elif tag is not None:
                unit, version = split_tag(tag)
                member = SELF_MEMBER
            entry = CodeEntry(
                fkey=new_fkey,
                params=tuple(fn.params),
                mutable_params=tuple(fn.mutable_params),
                body=body,
                free_names=tuple(fn.free_names),
                capture_kinds=tuple(fn.capture_kinds) if fn.capture_kinds else (),
                unit=unit, version=version, member=member,
                code_hash=expr_hash(body),
            )
            self.add_entry(entry)
        return remap

    def _index_version(self, entry: CodeEntry) -> None:
        assert entry.unit is not None and entry.version is not None
        key = (entry.unit, entry.version)
        members = self._versions.setdefault(key, {})
        assert entry.member is not None
        if entry.member in members and members[entry.member] != entry.fkey:
            raise CodeVersionError(
                f"unit {entry.unit!r} version {entry.version} member "
                f"{entry.member!r} registered twice with different functions"
            )
        members[entry.member] = entry.fkey
        vs = self._unit_versions.setdefault(entry.unit, [])
        if entry.version not in vs:
            vs.append(entry.version)
            vs.sort()

    # -- 查询 --------------------------------------------------------------

    def get(self, fkey: str) -> CodeEntry:
        try:
            return self._entries[fkey]
        except KeyError:
            raise CodeVersionError(f"unknown function key {fkey!r}")

    def has(self, fkey: str) -> bool:
        return fkey in self._entries

    def entries(self) -> list[CodeEntry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def members(self, unit: str, version: int) -> dict[str, CodeEntry]:
        table = self._versions.get((unit, version))
        if table is None:
            raise CodeVersionError(f"unit {unit!r} has no version {version}")
        return {m: self._entries[f] for m, f in table.items()}

    def versions(self, unit: str) -> list[int]:
        return list(self._unit_versions.get(unit, ()))

    def latest(self, unit: str) -> int:
        vs = self._unit_versions.get(unit)
        if not vs:
            raise CodeVersionError(f"unknown versioned unit {unit!r}")
        return vs[-1]

    def all_units(self) -> list[str]:
        return sorted(self._unit_versions)

    def member_entries(self, entry: CodeEntry) -> dict[str, CodeEntry]:
        """同单元同版本的全部成员表项（独立版本函数只含自身）。"""
        if entry.unit is None:
            return {SELF_MEMBER: entry}
        return self.members(entry.unit, entry.version)  # type: ignore[arg-type]

    def check_successor(self, old: CodeEntry, new: CodeEntry) -> None:
        """校验 new 是 old 同单元的合法后继版本（元数/成员集合口径）。"""
        if old.unit != new.unit or old.unit is None:
            raise CodeVersionError("successor must belong to the same versioned unit")
        if new.version != (old.version or 0) + 1:
            raise CodeVersionError(
                f"unit {old.unit!r}: versions must advance by exactly 1 "
                f"({old.version} -> {new.version})"
            )
        old_m = self.member_entries(old)
        new_m = self.member_entries(new)
        if set(old_m) != set(new_m):
            raise CodeVersionError(
                f"unit {old.unit!r}: member set changed across versions: "
                f"{sorted(old_m)} != {sorted(new_m)}"
            )
        for m in old_m:
            o, n = old_m[m], new_m[m]
            if o.arity != n.arity:
                raise CodeVersionError(
                    f"unit {old.unit!r} member {m!r}: arity changed "
                    f"{o.arity} -> {n.arity}"
                )
            if tuple(o.mutable_params) != tuple(n.mutable_params):
                raise CodeVersionError(
                    f"unit {old.unit!r} member {m!r}: parameter mutability "
                    f"changed across versions"
                )


def build_bundle(program: ast.Program) -> CodeBundle:
    """从转换输出构造版本表。"""
    return CodeBundle.from_program(program)


def _remap_closure_fids(e, remap: dict[str, str]):
    """深度复制 IR 表达式树，把 TMkClosure.fid 按 remap 重写。"""
    import dataclasses as _dc

    if isinstance(e, ast.TMkClosure):
        return ast.TMkClosure(
            fid=remap.get(e.fid, e.fid),
            env=tuple(_remap_closure_fids(x, remap) for x in e.env),
        )
    if not _dc.is_dataclass(e):
        if isinstance(e, tuple):
            return tuple(_remap_closure_fids(x, remap) for x in e)
        return e
    kwargs = {}
    for f in _dc.fields(e):
        v = getattr(e, f.name)
        if _dc.is_dataclass(v):
            kwargs[f.name] = _remap_closure_fids(v, remap)
        elif isinstance(v, tuple):
            kwargs[f.name] = tuple(
                _remap_closure_fids(x, remap) if _dc.is_dataclass(x) else x
                for x in v
            )
        else:
            kwargs[f.name] = v
    return type(e)(**kwargs)

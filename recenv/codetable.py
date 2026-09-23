"""版本化函数表。

``CodeTable`` 在 :class:`recenv.ast_nodes.Program` 的函数表之上提供多版本
视图：每个 ``fid`` 有一条版本链 ``v1, v2, ...``，栈帧/闭包用
``(fid, version)`` 坐标定位自己要执行的代码体。

* 旧栈帧继续引用其代码版本 —— 版本条目不可变，新版本只是追加；
* 新调用（``TMkClosure`` 总在当前帧代码中）经 ``current_version`` 表
  解析到该 fid 的当前版本；
* letrec 组的互递归调用在帧存活期间必须绑定到建组时的版本
  （语义：组内成员同时绑定、同步升级），由 :class:`~recenv.machine.Machine`
  通过帧上的 ``rec_version`` 解析，这里只负责存储。

版本条目同时携带该版本函数*激活帧*的字段名列表（无字段化信息时为 None），
供字段迁移使用；不改变 IR 本身。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import ImageFormatError, UpgradeValidationError


@dataclass(frozen=True)
class FunctionVersion:
    fid: str
    version: str
    fn: ast.TFunction
    fields: tuple[str, ...] | None  # 激活帧字段名（逐槽），None 表示未字段化


class CodeTable:
    def __init__(self, program: ast.Program) -> None:
        self.initial_version = program.code_version
        # fid -> {version -> FunctionVersion}
        self.versions: dict[str, dict[str, FunctionVersion]] = {}
        # fid -> 当前版本号
        self.current: dict[str, str] = {}
        for fid, fn in program.functions.items():
            fields: tuple[str, ...] | None = None
            sid = program.function_field_map.get(fid, {}).get(program.code_version)
            if sid is not None:
                fields = tuple(fs.field_name for fs in program.frame_schemas[sid])
            entry = FunctionVersion(fid, program.code_version, fn, fields)
            self.versions[fid] = {program.code_version: entry}
            self.current[fid] = program.code_version

    def get(self, fid: str, version: str) -> FunctionVersion:
        try:
            return self.versions[fid][version]
        except KeyError:
            raise ImageFormatError(f"code version {fid}@{version} not found") from None

    def current_version(self, fid: str) -> str:
        if fid not in self.current:
            raise UpgradeValidationError(f"unknown function id {fid!r}")
        return self.current[fid]

    def has(self, fid: str, version: str) -> bool:
        return fid in self.versions and version in self.versions[fid]

    def fids(self) -> list[str]:
        return sorted(self.versions)

    def add_version(
        self,
        fid: str,
        new_version: str,
        fn: ast.TFunction,
        fields: tuple[str, ...] | None,
    ) -> None:
        """追加一个版本（不切换 current；由 commit 阶段统一切换）。"""
        if fid not in self.versions:
            raise UpgradeValidationError(f"new function {fid!r}: only upgrades of existing fids allowed")
        if new_version in self.versions[fid]:
            raise UpgradeValidationError(
                f"version {new_version} of {fid!r} already exists"
            )
        self.versions[fid][new_version] = FunctionVersion(fid, new_version, fn, fields)

    def commit_current(self, targets: dict[str, str]) -> None:
        """原子切换：targets 中每个 fid 的 current 指向新版本。

        调用方必须保证新版本已 add_version；这里做最后一道防线校验。
        """
        for fid, ver in targets.items():
            if ver not in self.versions.get(fid, {}):
                raise UpgradeValidationError(
                    f"cannot publish {fid}@{ver}: version missing (commit aborted)"
                )
        self.current.update(targets)

    def snapshot(self) -> dict[str, str]:
        return dict(self.current)

    def restore(self, snap: dict[str, str]) -> None:
        self.current = dict(snap)

    def copy_for_staging(self) -> "CodeTable":
        """供迁移暂存用的结构复制（版本条目本身不可变，浅拷贝即可）。"""
        new = CodeTable.__new__(CodeTable)
        new.initial_version = self.initial_version
        new.versions = copy.deepcopy(self.versions)
        new.current = dict(self.current)
        return new

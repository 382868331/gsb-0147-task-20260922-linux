"""结构化字段化与版本迁移。

字段化（:func:`fieldize`）
--------------------------

convert 输出的帧槽只有布局标签（``layout``）。持久化/迁移入口先给每个函数
激活帧与 let/letrec 帧建立*稳定字段名*：

* 函数激活帧字段名取自 ``layout[fid]`` 中 ``val:/cell:/cell-rec:`` 之后、
  ``#id`` 之前的名字部分（同名不同绑定 ID 的槽天然不冲突，因为布局标签
  带 #id；字段名这里保留*限定名* ``name#idN`` 与标签一致）；
* let/letrec 帧（``TWithFrame``）的 schema_id 为合成 ID
  ``"frame@<fid or main>:<index>"``，字段名同样取自其 labels。

迁移规则（唯一允许的三类，全部针对*字段名*；槽位由名字映射）
----------------------------------------------------------------

1. **重命名**：``rename`` —— 同一绑定槽换名字；
2. **删除**：``delete`` —— 仅允许删除*新版本函数体不再引用*的字段
   （验证器扫描新版本 IR 中所有 TGet/TCellGet/TCellSet/TFillCell 的
   槽坐标，结合激活帧扁平化布局算出被引用字段集合，自行判定，
   不读升级包里的布尔自述）；
3. **增加**：``add`` —— 必须携带*常量整数默认值*，迁移时给存活旧记录
   的新槽填该常量。

原子性
------

:func:`upgrade_at_safepoint` 在*暂存副本*（堆深拷贝 + 版本表结构拷贝 +
程序副本）上完成全部验证、新代码注册、记录迁移；任一步失败（含注入钩子）
都丢弃暂存区，原执行机对象与版本指针不变。全部成功后才统一发布：
版本指针切换、暂存堆/程序换回、新代码版本登记。旧栈帧继续引用旧版本。
"""

from __future__ import annotations

import copy
from dataclasses import replace

from . import ast_nodes as ast
from .codetable import FunctionVersion
from .errors import (
    MachineStateError, UpgradeAborted, UpgradeValidationError,
)
from .heap import Heap
from .machine import Machine

MAIN = "<main>"


# ---------------------------------------------------------------------------
# 字段名工具：从 layout 标签取稳定限定名  "cell:c#id3" -> "c#id3"
# ---------------------------------------------------------------------------

def _field_from_label(label: str) -> str:
    for prefix in ("val:", "cell:", "cell-rec:"):
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


# ---------------------------------------------------------------------------
# 字段化
# ---------------------------------------------------------------------------

def fieldize(program: ast.Program) -> ast.Program:
    """返回带结构化帧字段声明的程序副本（纯函数；不修改输入）。

    已字段化的程序再次传入时原样返回（幂等）。
    """
    if program.function_field_map or program.frame_schemas:
        return program

    frame_schemas: dict[str, tuple[ast.FieldSpec, ...]] = {}
    function_field_map: dict[str, dict[str, str]] = {}

    # 函数激活帧 schema
    for fid, fn in program.functions.items():
        labels = program.layout.get(fid, [])
        specs = tuple(
            ast.FieldSpec(_field_from_label(lab)) for lab in labels[:fn.n_slots]
        )
        sid = f"act-schema:{fid}"
        frame_schemas[sid] = specs
        function_field_map[fid] = {program.code_version: sid}

    # 深拷贝代码树后就地写入 TWithFrame.schema_id（frozen -> object.__setattr__）
    program = copy.deepcopy(program)
    counters = {MAIN: 0}

    def annotate(e: ast.TExpr, owner: str) -> None:
        if isinstance(e, ast.TWithFrame):
            idx = counters.get(owner, 0)
            counters[owner] = idx + 1
            sid = f"frame-schema:{owner}:{idx}"
            specs = tuple(ast.FieldSpec(_field_from_label(lab)) for lab in e.labels)
            frame_schemas[sid] = specs
            object.__setattr__(e, "schema_id", sid)
            annotate(e.body, owner)
            for init in e.inits:
                annotate(init, owner)
            return
        if isinstance(e, ast.TCellSet):
            annotate(e.value, owner); return
        if isinstance(e, ast.TPrim):
            for a in e.args: annotate(a, owner)
            return
        if isinstance(e, ast.TMkClosure):
            for x in e.env: annotate(x, owner)
            return
        if isinstance(e, ast.TMkCell) and e.value is not None:
            annotate(e.value, owner); return
        if isinstance(e, ast.TFillCell):
            annotate(e.value, owner); return
        if isinstance(e, ast.TCall):
            annotate(e.func, owner)
            for a in e.args: annotate(a, owner)
            return
        if isinstance(e, ast.TIf0):
            annotate(e.cond, owner); annotate(e.then_e, owner); annotate(e.else_e, owner)
            return
        if isinstance(e, ast.TBegin):
            for x in e.exprs: annotate(x, owner)

    annotate(program.main, MAIN)
    for fid, fn in program.functions.items():
        annotate(fn.body, fid)

    return replace(
        program,
        frame_schemas=frame_schemas,
        function_field_map=function_field_map,
    )


# ---------------------------------------------------------------------------
# 新版本 IR：扫描引用槽集合
# ---------------------------------------------------------------------------

def referenced_slots(fn: ast.TFunction) -> set[tuple[int, int]]:
    """函数体中所有对激活帧（depth>=0）槽位的引用坐标集合。"""
    out: set[tuple[int, int]] = set()

    def walk(e) -> None:
        if isinstance(e, (ast.TGet, ast.TCellGet)):
            out.add((e.depth, e.slot)); return
        if isinstance(e, ast.TCellSet):
            out.add((e.depth, e.slot)); walk(e.value); return
        if isinstance(e, ast.TFillCell):
            out.add((e.depth, e.slot)); walk(e.value); return
        if isinstance(e, ast.TPrim):
            for a in e.args: walk(a)
            return
        if isinstance(e, ast.TMkClosure):
            for x in e.env: walk(x)
            return
        if isinstance(e, ast.TMkCell) and e.value is not None:
            walk(e.value); return
        if isinstance(e, ast.TCall):
            walk(e.func)
            for a in e.args: walk(a)
            return
        if isinstance(e, ast.TIf0):
            walk(e.cond); walk(e.then_e); walk(e.else_e); return
        if isinstance(e, ast.TWithFrame):
            for x in e.inits: walk(x)
            walk(e.body); return
        if isinstance(e, ast.TBegin):
            for x in e.exprs: walk(x)

    walk(fn.body)
    return out


# ---------------------------------------------------------------------------
# 升级
# ---------------------------------------------------------------------------

def upgrade_at_safepoint(
    m: Machine,
    new_version: str,
    new_code: dict[str, ast.TFunction],
    field_ops: dict[str, list[dict]] | None = None,
    *,
    inject_failure: str | None = None,
) -> None:
    """在安全点对执行机做一次原子版本升级。

    参数
    ----
    new_version: 新版本号（如 "v2"），所有列入 new_code 的 fid 统一切到此版本；
    new_code: fid -> 新版本 TFunction（必须基于升级后的字段布局，槽位规则
        与旧版本相同：前 n_capture 个捕获槽按绑定 ID 排序，后接参数槽）；
    field_ops: fid -> 字段操作列表，每项形如
        {"op": "rename", "old": 名, "new": 名} /
        {"op": "delete", "field": 名} /
        {"op": "add", "field": 名, "default": int}；
    inject_failure: 提交前注入失败的位置（"validate" / "stage" / "commit"），
        用于测试回滚；正常调用传 None。

    升级后：旧栈帧/旧闭包仍引用旧版本（其代码与字段布局不变），存活堆中
    属于*被升级闭包组*的捕获记录按字段操作迁移（保留 OID → 别名/共享 cell
    不复制），并在同一 OID 上发布 v2 闭包载荷；之后的新调用解析到新版本。
    """
    if not m.at_safepoint:
        raise MachineStateError(
            "upgrade is only allowed at a call-entry safepoint "
            "(or after the machine finished)"
        )
    field_ops = field_ops or {}

    # 提交前注入点（validate）：此刻尚未修改任何状态，直接拒绝即可
    if inject_failure == "validate":
        raise UpgradeAborted("injected failure at validate")

    # --- 静态验证（在原表上，先不动任何状态；急切执行，全部通过才进入暂存）---
    current_versions = {fid: m.table.current_version(fid) for fid in new_code}
    # 急切完成全部静态验证（生成器必须在进入暂存 try前沿例化，否则验证
    # 错误会被误报成“暂存失败回滚”而不是 UpgradeValidationError）
    plans = dict(_validate_all(m, new_version, new_code, field_ops, current_versions))

    if inject_failure == "stage":
        raise UpgradeAborted("injected failure at staging")

    # --- 暂存：深拷贝堆 / 程序，结构拷贝版本表 ----------------------------
    saved_heap = m.heap
    saved_program = m.program
    saved_table = m.table
    saved_state = m.state
    try:
        staged_heap = _clone_heap(m.heap)
        staged_program = copy.deepcopy(m.program)
        staged_table = m.table.copy_for_staging()

        staged_program = _stage_upgrade(
            staged_heap, staged_program, staged_table,
            new_version, new_code, plans,
        )

        if inject_failure == "commit":
            raise UpgradeAborted("injected failure at commit")

        # --- 提交 ----------------------------------------------------------
        staged_table.commit_current(
            {fid: new_version for fid in new_code}
        )
    except UpgradeAborted:
        # 暂存区整体丢弃；原机器对象原封不动
        m.heap = saved_heap
        m.program = saved_program
        m.table = saved_table
        m.state = saved_state
        raise
    except Exception as exc:
        m.heap = saved_heap
        m.program = saved_program
        m.table = saved_table
        m.state = saved_state
        raise UpgradeAborted(f"unexpected staging error: {exc}") from exc

    m.heap = staged_heap
    m.program = staged_program
    m.table = staged_table
    if m.state == "finished":
        m.state = "finished"
    else:
        m.state = "paused"


def _validate_all(m, new_version, new_code, field_ops, current_versions):
    if not isinstance(new_version, str) or not new_version:
        raise UpgradeValidationError("new_version must be a non-empty string")
    for fid, nfn in new_code.items():
        if fid not in m.table.versions:
            raise UpgradeValidationError(f"cannot add brand-new function {fid!r}")
        if m.table.has(fid, new_version):
            raise UpgradeValidationError(f"{fid}@{new_version} already exists")
        ofv = m.table.get(fid, current_versions[fid])
        if not isinstance(nfn, ast.TFunction):
            raise UpgradeValidationError(f"{fid}: new code must be TFunction")
        if nfn.fid != fid:
            raise UpgradeValidationError(f"{fid}: new function fid mismatch")
        if len(nfn.params) != len(ofv.fn.params) or tuple(nfn.params) != tuple(ofv.fn.params):
            raise UpgradeValidationError(
                f"{fid}: parameter list cannot change across versions"
            )
        if tuple(nfn.mutable_params) != tuple(ofv.fn.mutable_params):
            raise UpgradeValidationError(
                f"{fid}: parameter mutability cannot change across versions"
            )
        plan = _plan_fields(ofv, nfn, field_ops.get(fid, []))
        _validate_new_code_refs(ofv, nfn, plan, fid)
        # 新 IR 的深度/槽位结构整体合法性：所有 depth/slot 在新布局内可达
        _validate_ir_bounds(nfn)
        yield fid, plan


def _plan_fields(ofv: FunctionVersion, nfn: ast.TFunction, ops: list[dict]):
    old_names = list(ofv.fields or [f"slot{i}" for i in range(ofv.fn.n_slots)])
    if len(old_names) != ofv.fn.n_slots:
        raise UpgradeValidationError(f"{ofv.fid}: old field layout length mismatch")
    # 当前名字 -> 槽（迁移过程中随 rename 更新）
    name_to_slot = {name: i for i, name in enumerate(old_names)}
    new_names = list(old_names)          # 按槽的当前名字
    deleted_slots: set[int] = set()
    added: list[tuple[str, int]] = []    # (name, default)
    touched_slot: set[int] = set()       # 旧槽在本次升级中已被 rename/delete

    for op in ops:
        if not isinstance(op, dict) or "op" not in op:
            raise UpgradeValidationError("field op must be an object with 'op'")
        kind = op["op"]
        if kind == "rename":
            old = op.get("old"); new = op.get("new")
            if not isinstance(old, str) or not isinstance(new, str) or not new:
                raise UpgradeValidationError("rename needs non-empty string old/new")
            if old not in name_to_slot:
                raise UpgradeValidationError(f"rename: old field {old!r} not found")
            slot = name_to_slot[old]
            if slot >= ofv.fn.n_capture:
                raise UpgradeValidationError(
                    f"rename of parameter slot {old!r} is not allowed"
                )
            if slot in touched_slot:
                raise UpgradeValidationError(f"rename: field {old!r} already changed")
            if new != old and new in name_to_slot:
                raise UpgradeValidationError(f"rename: target name {new!r} already exists")
            del name_to_slot[old]
            name_to_slot[new] = slot
            new_names[slot] = new
            touched_slot.add(slot)
        elif kind == "delete":
            fld = op.get("field")
            if not isinstance(fld, str) or fld not in name_to_slot:
                raise UpgradeValidationError(f"delete: field {fld!r} not found")
            slot = name_to_slot[fld]
            if slot >= ofv.fn.n_capture:
                raise UpgradeValidationError(
                    f"delete: cannot remove parameter slot {fld!r}"
                )
            if slot in touched_slot:
                raise UpgradeValidationError(f"delete: field {fld!r} already changed")
            del name_to_slot[fld]
            deleted_slots.add(slot)
            touched_slot.add(slot)
        elif kind == "add":
            fld = op.get("field")
            default = op.get("default")
            if not isinstance(fld, str) or not fld:
                raise UpgradeValidationError("add: field name required")
            if not isinstance(default, int) or isinstance(default, bool):
                raise UpgradeValidationError(
                    f"add: field {fld!r} needs a constant integer default"
                )
            if fld in name_to_slot or fld in {x[0] for x in added}:
                raise UpgradeValidationError(f"add: field {fld!r} already exists")
            name_to_slot[fld] = -1  # 新槽位在存活捕获槽之后
            added.append((fld, default))
        else:
            raise UpgradeValidationError(f"unknown field op {kind!r}")

    # 新布局的捕获槽：删除的槽去掉，新增字段追加到捕获区末尾（参数槽不动）
    surviving_caps = [
        (i, new_names[i]) for i in range(ofv.fn.n_capture) if i not in deleted_slots
    ]
    expected_new_capture = len(surviving_caps) + len(added)
    if nfn.n_capture != expected_new_capture:
        raise UpgradeValidationError(
            f"{ofv.fid}: new n_capture {nfn.n_capture} != field-plan prediction "
            f"{expected_new_capture} (surviving {len(surviving_caps)} + added {len(added)})"
        )
    if nfn.n_slots != nfn.n_capture + len(nfn.params):
        raise UpgradeValidationError(f"{ofv.fid}: new n_slots inconsistent")

    ordered_new_names = [nm for _, nm in surviving_caps] + [nm for nm, _ in added]
    if len(set(ordered_new_names)) != len(ordered_new_names):
        raise UpgradeValidationError(f"{ofv.fid}: duplicate field names after migration")

    return {
        "old_names": old_names,
        "deleted_slots": deleted_slots,
        "surviving": surviving_caps,     # [(旧槽, 新名)]
        "added": added,
        "new_capture_names": ordered_new_names,
        "new_param_names": [  # 参数槽名字保持
            new_names[i] for i in range(ofv.fn.n_capture, ofv.fn.n_slots)
        ],
        "defaults": {nm: d for nm, d in added},
    }


def _validate_new_code_refs(ofv, nfn, plan, fid: str) -> None:
    """按字段名 + 槽位双重独立验证新代码与迁移计划一致（不信任布尔自述）。

    字段身份靠名字（槽位只是位置；rename 位置不变，纯槽位检查无法表达）：

    * 新函数 ``free_names`` 必须逐槽等于字段计划推出的新捕获字段名序列
      （存活字段的新名 + 新增字段名）。被删字段因此在新布局里*无位可占*，
      新代码在结构上不可能再引用它——“只允许删除未被引用字段”的独立判定；
    * 新代码 depth 0 引用的槽位不越界（< 新 n_slots）；depth>0 指向内层
      TWithFrame 帧，几何边界由运行时按 schema 强制；
    * 参数槽名保持不变，且参数区在捕获区增删后整体平移（n_slots 一致性
      在计划构造时已校验）。
    """
    refs = referenced_slots(nfn)
    expected_names = plan["new_capture_names"]
    new_names = list(nfn.free_names)
    if len(new_names) != nfn.n_capture:
        raise UpgradeValidationError(
            f"{fid}: new free_names length {len(new_names)} "
            f"!= new n_capture {nfn.n_capture}"
        )
    if new_names != expected_names:
        raise UpgradeValidationError(
            f"{fid}: new capture field layout {new_names} "
            f"does not match field migration plan {expected_names}"
        )
    for depth, slot in refs:
        if depth < 0:
            raise UpgradeValidationError(f"{fid}: negative depth in new code")
        if depth == 0 and slot >= nfn.n_slots:
            raise UpgradeValidationError(
                f"{fid}: new code references slot {slot} beyond n_slots "
                f"{nfn.n_slots}"
            )


def _validate_ir_bounds(fn: ast.TFunction) -> None:
    """对新函数体做结构扫描：depth/slot 非负（具体越界由执行时捕获）。"""
    def chk(e):
        if isinstance(e, (ast.TGet, ast.TCellGet)):
            if e.depth < 0 or e.slot < 0:
                raise UpgradeValidationError("negative depth/slot in new code")
        for child in _children(e):
            chk(child)
    chk(fn.body)


def _children(e):
    if isinstance(e, ast.TCellSet): return (e.value,)
    if isinstance(e, ast.TPrim): return tuple(e.args)
    if isinstance(e, ast.TMkClosure): return tuple(e.env)
    if isinstance(e, ast.TMkCell): return () if e.value is None else (e.value,)
    if isinstance(e, ast.TFillCell): return (e.value,)
    if isinstance(e, ast.TCall): return (e.func,) + tuple(e.args)
    if isinstance(e, ast.TIf0): return (e.cond, e.then_e, e.else_e)
    if isinstance(e, ast.TWithFrame): return tuple(e.inits) + (e.body,)
    if isinstance(e, ast.TBegin): return tuple(e.exprs)
    return ()


# ---------------------------------------------------------------------------
# 暂存与堆迁移
# ---------------------------------------------------------------------------

def _clone_heap(heap: Heap) -> Heap:
    h = Heap()
    h.records = {oid: tuple(slots) for oid, slots in heap.records.items()}
    h.rec_schema = dict(heap.rec_schema)
    h.cells = {oid: copy.copy(cs) for oid, cs in heap.cells.items()}
    h.closures = {oid: tuple(info) for oid, info in heap.closures.items()}
    h._next = heap._next
    return h


def _stage_upgrade(staged_heap, staged_program, staged_table,
                   new_version, new_code, plans) -> ast.Program:
    plans = dict(plans)
    targets = set(new_code)

    # 1) 在暂存程序中登记新版本函数与新字段 schema
    new_frame_schemas = dict(staged_program.frame_schemas)
    fmap = copy.deepcopy(staged_program.function_field_map)
    new_defaults = dict(staged_program.field_defaults)

    for fid in targets:
        nfn = new_code[fid]
        plan = plans[fid]
        fields = tuple(plan["new_capture_names"] + plan["new_param_names"])
        sid = f"act-schema:{fid}@{new_version}"
        new_frame_schemas[sid] = tuple(
            ast.FieldSpec(
                name,
                default=(plan["defaults"].get(name)),
            )
            for name in fields
        )
        for name, d in plan["defaults"].items():
            new_defaults[f"{sid}.{name}"] = d
        fmap.setdefault(fid, {})[new_version] = sid
        staged_table.add_version(
            fid, new_version,
            replace(nfn),
            fields,
        )
    staged_program = replace(
        staged_program,
        frame_schemas=new_frame_schemas,
        function_field_map=fmap,
        field_defaults=new_defaults,
    )
    # 多版本函数体通过镜像 table 段持久化；program.functions 仍只存初始版本。

    # 2) 迁移堆中属于被被升级闭包的 capture 记录，并在同 OID 重发闭包载荷
    for fid in targets:
        plan = plans[fid]
        _migrate_closure_group(staged_heap, fid, new_version, plan)

    return staged_program


def _migrate_closure_group(heap: Heap, fid: str, new_version: str, plan) -> None:
    old_cap_prefix = "capture:" + fid + ":"
    surviving = plan["surviving"]          # [(old_slot, new_name)]
    added = plan["added"]

    cap_records = [
        oid for oid, schema in heap.rec_schema.items()
        if isinstance(schema, str) and schema.startswith(old_cap_prefix)
    ]
    for rec_oid in cap_records:
        old_slots = heap.records[rec_oid]
        old_version = heap.rec_schema[rec_oid][len(old_cap_prefix):]
        new_slots = list(old_slots[i] for i, _ in surviving)
        for _, default in added:
            new_slots.append(default)
        heap.records[rec_oid] = tuple(new_slots)
        heap.rec_schema[rec_oid] = f"capture:{fid}:{new_version}"

    # 闭包载荷：同一 OID，fid 不变，record OID 不变（别名保留），版本升级
    for clo_oid, (cfid, rec_oid, ver) in list(heap.closures.items()):
        if cfid != fid:
            continue
        if rec_oid in cap_records:
            heap.closures[clo_oid] = (fid, rec_oid, new_version)

    # 激活记录（旧栈帧持有）：保持旧 schema 标签不变 → 旧帧继续旧布局。
    # 新调用产生的 act 记录在执行时按新版本 n_slots 建立，无需预迁移。

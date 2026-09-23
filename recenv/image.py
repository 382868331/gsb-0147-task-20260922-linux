"""执行镜像：显式堆 + 控制栈 + 多版本代码的确定性 JSON 序列化。

格式标签：``"recenv-image/1"``（独立版本，旧接口不受影响）。

关键性质
--------

* 堆对象（record / cell / closure）以稳定 OID 为键，引用一律是 OID 字符串；
  加载分两阶段（先登记全部对象、再校验引用），因此**共享与环原样保留**
  （letrec 组帧 cell ↔ 成员闭包即环），绝不会把别名复制成不同对象。
* 镜像中对象条目的排列顺序不影响装载结果；装载器不保留任何 Python 对象
  id，身份完全由 OID 决定（见置换测试）。
* 栈帧不存代码副本，只存 ``(fid, version)`` + 代码坐标路径；装载时从
  版本表取回代码体，沿路径重新定位并与记录的节点标签交叉核对。
* 装载在发布前做完整引用图校验：悬空引用、类型不符、槽数不符、
  代码版本缺失、续延坐标越界等全部抛 :class:`ImageFormatError`，
  校验器不读取镜像内任何“我合法”的布尔自述。
"""

from __future__ import annotations

import json

from . import ast_nodes as ast
from . import ircode
from .codetable import CodeTable
from .errors import ImageFormatError
from .heap import Heap, OID_PREFIX, UNFILLED
from .machine import Frame, Machine, MAIN_CODE

FORMAT = "recenv-image/1"

# 续延标签 -> 其 np 坐标必须指向的 IR 节点类型
_NP_TYPES = {
    "call_func": ast.TCall,
    "call_arg": ast.TCall,
    "prim": ast.TPrim,
    "if_k": ast.TIf0,
    "begin": ast.TBegin,
    "inits": ast.TWithFrame,
    "clo_env": ast.TMkClosure,
}
# 续延中标量路径坐标的键
_PATH_KEYS = ("np",)
_VALUE_LIST_KEYS = ("argv", "acc")


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def _table_to_obj(table: CodeTable) -> dict:
    funcs: dict[str, dict[str, dict]] = {}
    for fid, vmap in table.versions.items():
        funcs[fid] = {
            ver: ircode._function_to_obj(fv.fn) for ver, fv in vmap.items()
        }
    return {"versions": funcs, "current": dict(table.current)}


def dump_machine(m: Machine) -> dict:
    """把暂停（或结束）状态的执行机导出为可 json 序列化的纯数据字典。"""
    if m.state == "running":
        from .errors import MachineStateError
        raise MachineStateError("cannot dump a running machine")
    heap = m.heap
    # 镜像要求每条记录都有结构化 schema；非字段化程序（schema None）不允许
    bad = [oid for oid, s in heap.rec_schema.items() if s is None]
    if bad:
        from .errors import MachineStateError
        raise MachineStateError(
            "cannot dump a machine whose program was not fieldized "
            f"({len(bad)} records have no schema); use migration.fieldize()"
        )

    records = {}
    for oid, slots in heap.records.items():
        records[oid] = {
            "slots": list(slots),
            "schema": heap.rec_schema.get(oid),
        }
    cells = {}
    for oid, cs in heap.cells.items():
        cells[oid] = {
            "filled": cs.filled,
            "value": None if (not cs.filled and cs.value is None) else cs.value,
            "unfilled": not cs.filled,
        }
    closures = {
        oid: {"fid": fid, "record": rec, "version": ver}
        for oid, (fid, rec, ver) in heap.closures.items()
    }

    frames = []
    for f in m.stack:
        root = m.code_root(f.code)
        here = m.code_at(root, f.path)
        frames.append({
            "kind": f.kind,
            "record": f.record,
            "code": list(f.code),
            "fid": f.fid,
            "version": f.version,
            "path": list(f.path),
            "term_tag": type(here).__name__,
            "k": _jsonable(f.k),
            "count_steps": f.count_steps,
            "call_entry": f.call_entry,
            "versions_view": dict(f.versions_view) if f.versions_view else None,
        })

    return {
        "format": FORMAT,
        "code": {
            "program": ircode.program_to_obj(m.program),
            "table": _table_to_obj(m.table),
        },
        "data": {
            "records": records,
            "cells": cells,
            "closures": closures,
        },
        "control": {
            "frames": frames,
            "steps": m.steps,
            "step_limit": m.step_limit,
            "events": [list(ev) for ev in m.events],
            "state": m.state,
            "result": m.result,
        },
    }


def _jsonable(v):
    if isinstance(v, dict):
        return {kk: _jsonable(vv) for kk, vv in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def to_json(m: Machine, *, indent: int | None = None) -> str:
    return json.dumps(dump_machine(m), ensure_ascii=False, indent=indent, sort_keys=True)


def save_image(m: Machine, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(to_json(m))


# ---------------------------------------------------------------------------
# 装载
# ---------------------------------------------------------------------------

def _is_ref(v) -> bool:
    return isinstance(v, str) and v.startswith(OID_PREFIX) and v != UNFILLED


def _validate_value(v, path: str) -> None:
    if isinstance(v, bool):
        raise ImageFormatError(f"{path}: bool is not a legal value")
    if v is None or isinstance(v, int):
        return
    if isinstance(v, str):
        if not v.startswith(OID_PREFIX):
            raise ImageFormatError(f"{path}: string values must be OIDs, got {v!r}")
        return
    raise ImageFormatError(f"{path}: bad value type {type(v).__name__}")


def load_machine(obj: dict) -> Machine:
    """完整校验并重建执行机；任何图/代码不一致都抛 ImageFormatError。"""
    if not isinstance(obj, dict) or obj.get("format") != FORMAT:
        raise ImageFormatError(f"not a {FORMAT} image")
    code = obj.get("code")
    data = obj.get("data")
    ctrl = obj.get("control")
    if not isinstance(code, dict) or not isinstance(data, dict) \
            or not isinstance(ctrl, dict):
        raise ImageFormatError("image: missing code/data/control sections")

    program, table = _load_code(code)
    heap = _load_data(data, table, program)
    machine = _load_control(ctrl, program, table, heap)
    return machine


def _load_code(code: dict):
    prog_obj = code.get("program")
    table_obj = code.get("table")
    if not isinstance(prog_obj, dict) or not isinstance(table_obj, dict):
        raise ImageFormatError("code: program/table sections required")
    program = ircode.program_from_obj(prog_obj)

    table = CodeTable(program)
    versions = table_obj.get("versions")
    current = table_obj.get("current")
    if not isinstance(versions, dict) or not isinstance(current, dict):
        raise ImageFormatError("code.table: versions/current maps required")
    # 登记额外版本（升级产生的 v2...）
    for fid, vmap in versions.items():
        if not isinstance(fid, str) or not isinstance(vmap, dict):
            raise ImageFormatError("code.table.versions: bad entry")
        if fid not in table.versions:
            raise ImageFormatError(f"code.table: unknown fid {fid!r}")
        for ver, fobj in vmap.items():
            if not isinstance(ver, str):
                raise ImageFormatError("code.table: version must be string")
            if table.has(fid, ver):
                # 与 bootstrap 版本逐条一致校验
                fn_loaded = ircode._function_from_obj(fobj, f"version {fid}@{ver}")
                have = table.get(fid, ver).fn
                if fn_loaded != have:
                    raise ImageFormatError(
                        f"code.table: duplicated inconsistent definition {fid}@{ver}"
                    )
                continue
            fn = ircode._function_from_obj(fobj, f"version {fid}@{ver}")
            if fn.fid != fid:
                raise ImageFormatError(f"code.table: fid mismatch in {fid}@{ver}")
            fields = _fields_for(program, fid, ver)
            table.versions[fid][ver] = _fv(fid, ver, fn, fields)
    for fid, ver in current.items():
        if not isinstance(fid, str) or not isinstance(ver, str):
            raise ImageFormatError("code.table.current: str->str expected")
        if not table.has(fid, ver):
            raise ImageFormatError(f"code.table.current: {fid}@{ver} missing")
        table.current[fid] = ver

    # 跨版本字段 schema 长度必须与对应版本函数 n_slots 一致
    for fid, vermap in program.function_field_map.items():
        for ver, sid in vermap.items():
            if not table.has(fid, ver):
                raise ImageFormatError(
                    f"code.function_field_map: code {fid}@{ver} missing"
                )
            fn = table.get(fid, ver).fn
            specs = program.frame_schemas[sid]
            if len(specs) != fn.n_slots:
                raise ImageFormatError(
                    f"code.function_field_map: schema {sid!r} has {len(specs)} "
                    f"fields but {fid}@{ver} has n_slots {fn.n_slots}"
                )
            names = [fs.field_name for fs in specs]
            if len(set(names)) != len(names):
                raise ImageFormatError(f"code: duplicate field names in {sid!r}")
    return program, table


def _fv(fid, ver, fn, fields):
    from .codetable import FunctionVersion
    return FunctionVersion(fid, ver, fn, fields)


def _fields_for(program: ast.Program, fid: str, ver: str) -> tuple[str, ...] | None:
    vermap = program.function_field_map.get(fid)
    if not vermap:
        return None
    sid = vermap.get(ver)
    if sid is None:
        return None
    return tuple(fs.field_name for fs in program.frame_schemas[sid])


def _load_data(data: dict, table: CodeTable, program: ast.Program) -> Heap:
    records = data.get("records")
    cells = data.get("cells")
    closures = data.get("closures")
    if not isinstance(records, dict) or not isinstance(cells, dict) \
            or not isinstance(closures, dict):
        raise ImageFormatError("data: records/cells/closures maps required")

    oids: set[str] = set()
    for group in (records, cells, closures):
        for oid in group:
            Heap._check_oid(oid)
            if oid in oids:
                raise ImageFormatError(f"data: duplicate OID across groups: {oid}")
            oids.add(oid)

    heap = Heap()

    # 阶段一：登记全部对象（引用暂不解析）
    rec_slots: dict[str, list] = {}
    for oid, entry in records.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("slots"), list):
            raise ImageFormatError(f"data.records[{oid}]: slots list required")
        schema = entry.get("schema")
        if schema is not None and not isinstance(schema, str):
            raise ImageFormatError(f"data.records[{oid}].schema: string or null")
        slots = entry["slots"]
        for i, s in enumerate(slots):
            _validate_value(s, f"data.records[{oid}].slots[{i}]")
        rec_slots[oid] = slots
        heap.register_record(oid, tuple(slots), schema)

    for oid, entry in cells.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("filled"), bool):
            raise ImageFormatError(f"data.cells[{oid}]: filled bool required")
        unfilled = entry.get("unfilled", False)
        if not isinstance(unfilled, bool):
            raise ImageFormatError(f"data.cells[{oid}].unfilled: bool")
        filled = entry["filled"]
        val = entry.get("value")
        if unfilled != (not filled):
            raise ImageFormatError(f"data.cells[{oid}]: filled/unfilled disagree")
        if not filled:
            if val is not None:
                raise ImageFormatError(f"data.cells[{oid}]: unfilled cell must have null value")
            val = UNFILLED
        else:
            if val is None:
                raise ImageFormatError(f"data.cells[{oid}]: filled cell must carry int or OID")
            _validate_value(val, f"data.cells[{oid}].value")
        heap.register_cell(oid, val, filled)

    clo_info: dict[str, tuple[str, str, str]] = {}
    for oid, entry in closures.items():
        if not isinstance(entry, dict):
            raise ImageFormatError(f"data.closures[{oid}]: object required")
        fid = entry.get("fid")
        rec = entry.get("record")
        ver = entry.get("version")
        if not all(isinstance(x, str) for x in (fid, rec, ver)):
            raise ImageFormatError(f"data.closures[{oid}]: fid/record/version strings")
        clo_info[oid] = (fid, rec, ver)
        heap.register_closure(oid, fid, rec, ver)  # record 存在性在阶段二校验

    # 阶段二：完整引用图校验
    def require(oid: str, where: str):
        if oid not in oids:
            raise ImageFormatError(f"{where}: dangling reference {oid}")

    for oid, slots in rec_slots.items():
        for i, s in enumerate(slots):
            if _is_ref(s):
                require(s, f"data.records[{oid}].slots[{i}]")
    for oid, entry in cells.items():
        if entry["filled"]:
            v = entry.get("value")
            if _is_ref(v):
                require(v, f"data.cells[{oid}].value")
    for oid, (fid, rec, ver) in clo_info.items():
        require(rec, f"data.closures[{oid}].record")
        if rec not in rec_slots:
            raise ImageFormatError(f"data.closures[{oid}]: record {rec} is not a record")
        if not table.has(fid, ver):
            raise ImageFormatError(f"data.closures[{oid}]: code {fid}@{ver} missing")
        fn = table.get(fid, ver).fn
        schema = heap.rec_schema[rec]
        n = len(heap.records[rec])
        if schema == f"capture:{fid}:{ver}":
            if n != fn.n_capture:
                raise ImageFormatError(
                    f"data.closures[{oid}]: capture record has {n} slots, "
                    f"expected {fn.n_capture}"
                )
        elif schema == f"act:{fid}:{ver}":
            if n != fn.n_slots:
                raise ImageFormatError(
                    f"data.closures[{oid}]: schema {schema} record length {n} "
                    f"!= n_slots {fn.n_slots}"
                )
        else:
            raise ImageFormatError(
                f"data.closures[{oid}]: record {rec} schema {schema!r} "
                f"must be capture:{fid}:{ver}"
            )

    # act/capture 记录的存在性/槽数/代码版本全局校验（含不可达的运行垃圾：
    # 堆是单调分配日志，已返回激活帧的临时对象允许留在镜像里）
    for oid, schema in heap.rec_schema.items():
        if not isinstance(schema, str):
            continue
        n = len(heap.records[oid])
        if schema.startswith("act:"):
            _, fid, ver = schema.split(":", 2)
            if not table.has(fid, ver):
                raise ImageFormatError(f"data.records[{oid}]: code {fid}@{ver} missing")
            if n != table.get(fid, ver).fn.n_slots:
                raise ImageFormatError(
                    f"data.records[{oid}]: activation length {n} != n_slots"
                )
        elif schema.startswith("capture:"):
            _, fid, ver = schema.split(":", 2)
            if not table.has(fid, ver):
                raise ImageFormatError(f"data.records[{oid}]: code {fid}@{ver} missing")
            if n != table.get(fid, ver).fn.n_capture:
                raise ImageFormatError(
                    f"data.records[{oid}]: capture length {n} != n_capture"
                )
        elif schema.startswith("frame-schema:"):
            prog_sid = schema
            if prog_sid not in program.frame_schemas:
                raise ImageFormatError(f"data.records[{oid}]: unknown schema {schema!r}")
            if n != len(program.frame_schemas[prog_sid]):
                raise ImageFormatError(f"data.records[{oid}]: frame record length mismatch")
        else:
            raise ImageFormatError(
                f"data.records[{oid}]: bad record schema tag {schema!r}"
            )

    return heap


def _load_control(ctrl: dict, program: ast.Program, table: CodeTable,
                  heap: Heap) -> Machine:
    frames_raw = ctrl.get("frames")
    if not isinstance(frames_raw, list) or not frames_raw:
        raise ImageFormatError("control.frames: non-empty list required")
    steps = ctrl.get("steps")
    step_limit = ctrl.get("step_limit")
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 0:
        raise ImageFormatError("control.steps: non-negative int")
    if not isinstance(step_limit, int) or isinstance(step_limit, bool) or step_limit < 0:
        raise ImageFormatError("control.step_limit: non-negative int")
    events_raw = ctrl.get("events", [])
    if not isinstance(events_raw, list):
        raise ImageFormatError("control.events: list")
    events = []
    for ev in events_raw:
        if not (isinstance(ev, list) and len(ev) == 3 and ev[0] == "set"
                and isinstance(ev[1], str) and isinstance(ev[2], int)
                and not isinstance(ev[2], bool)):
            raise ImageFormatError("control.events: bad event")
        events.append(("set", ev[1], ev[2]))
    state = ctrl.get("state")
    if state not in ("paused", "finished", "error"):
        raise ImageFormatError(f"control.state: bad value {state!r}")
    result = ctrl.get("result")
    if result is not None and (not isinstance(result, int) or isinstance(result, bool)):
        raise ImageFormatError("control.result: int or null")

    m = Machine(program, step_limit=step_limit, heap=heap, table=table,
                events=events, steps=steps)
    m.stack = []
    root_values: list[object] = []
    for i, fobj in enumerate(frames_raw):
        m.stack.append(_load_frame(fobj, f"control.frames[{i}]", m, root_values))

    # 根完整性：从全部栈帧记录与续延值出发必须能解析（无悬空引用）。
    # 不可达对象允许保留（单调分配的堆日志），但它们的结构已在
    # _load_data 中逐条校验，不会因为不可达就逃过检查。
    for f in m.stack:
        if f.record is not None:
            root_values.append(f.record)
        for v in _continuation_roots(f.k):
            root_values.append(v)
    try:
        heap.reachable(root_values)
    except Exception as exc:  # 悬空根
        raise ImageFormatError(f"data: dangling stack root: {exc}") from None

    m.state = "paused" if state == "error" else state
    m.result = result if state == "finished" else None
    if state == "finished":
        if not m.stack or m.stack[-1].k.get("k") != "halt":
            raise ImageFormatError("control: finished state but top frame not at halt")
    return m


def _continuation_roots(k: dict) -> list:
    out = []
    tag = k.get("k")
    if "fn" in k and isinstance(k["fn"], str):
        out.append(k["fn"])
    if "argv" in k:
        out.extend(k["argv"])
    # 仅 inits / clo_env 的 acc 装着堆值（prim 的 acc 是 int 累加器）
    if tag in ("inits", "clo_env") and "acc" in k:
        out.extend(k["acc"])
    inner = k.get("inner")
    if isinstance(inner, dict):
        out.extend(_continuation_roots(inner))
    return out


def _load_frame(fobj: dict, path: str, m: Machine, root_values: list) -> Frame:
    if not isinstance(fobj, dict):
        raise ImageFormatError(f"{path}: frame must be object")
    kind = fobj.get("kind")
    if kind not in ("main", "frame", "act"):
        raise ImageFormatError(f"{path}.kind: bad value {kind!r}")
    record = fobj.get("record")
    code_raw = fobj.get("code")
    fid = fobj.get("fid")
    version = fobj.get("version")
    fpath = fobj.get("path")
    term_tag = fobj.get("term_tag")
    kobj = fobj.get("k")
    count_steps = fobj.get("count_steps")
    call_entry = fobj.get("call_entry")
    view_raw = fobj.get("versions_view")
    if not isinstance(fpath, list) or not isinstance(term_tag, str):
        raise ImageFormatError(f"{path}: path/list and term_tag/string required")
    if not isinstance(kobj, dict):
        raise ImageFormatError(f"{path}.k: object required")
    if not isinstance(count_steps, bool) or not isinstance(call_entry, bool):
        raise ImageFormatError(f"{path}: count_steps/call_entry must be bool")
    if view_raw is not None and not (
        isinstance(view_raw, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in view_raw.items())
    ):
        raise ImageFormatError(f"{path}.versions_view: str->str object or null")
    versions_view = dict(view_raw) if view_raw else None
    if versions_view is not None:
        for vfid, vver in versions_view.items():
            if not m.table.has(vfid, vver):
                raise ImageFormatError(
                    f"{path}.versions_view: code {vfid}@{vver} missing"
                )
    if not isinstance(code_raw, list) or not code_raw:
        raise ImageFormatError(f"{path}.code: non-empty list")

    if kind == "main":
        if tuple(code_raw) != MAIN_CODE or record is not None \
                or fid is not None or version is not None:
            raise ImageFormatError(f"{path}: bad main frame")
        code = MAIN_CODE
    elif kind == "act":
        if tuple(code_raw[:1]) != ("fn",) or len(code_raw) != 3 \
                or fid != code_raw[1] or version != code_raw[2]:
            raise ImageFormatError(f"{path}: inconsistent act code/fid/version")
        if not isinstance(fid, str) or not isinstance(version, str):
            raise ImageFormatError(f"{path}: fid/version strings")
        if not m.table.has(fid, version):
            raise ImageFormatError(f"{path}: code {fid}@{version} missing")
        code = ("fn", fid, version)
        if not isinstance(record, str) or record not in m.heap.records:
            raise ImageFormatError(f"{path}: missing activation record {record}")
        if m.heap.rec_schema[record] != f"act:{fid}:{version}":
            raise ImageFormatError(f"{path}: record schema mismatch")
    else:  # frame：TWithFrame 控制帧，继承所在代码（可能是 main 或函数版本）
        if tuple(code_raw) == MAIN_CODE:
            if fid is not None or version is not None:
                raise ImageFormatError(f"{path}: main-code frame must carry no fid/version")
            code = MAIN_CODE
        else:
            if tuple(code_raw[:1]) != ("fn",) or len(code_raw) != 3 \
                    or fid != code_raw[1] or version != code_raw[2]:
                raise ImageFormatError(f"{path}: inconsistent frame code")
            if not isinstance(fid, str) or not isinstance(version, str):
                raise ImageFormatError(f"{path}: fid/version strings")
            if not m.table.has(fid, version):
                raise ImageFormatError(f"{path}: code {fid}@{version} missing")
            code = ("fn", fid, version)
        if record is not None and record not in m.heap.records:
            raise ImageFormatError(f"{path}: dangling record {record}")

    # 代码坐标核对：从版本表取代码体，沿 path 重新定位，标签必须吻合
    root = m.code_root(code)
    try:
        term = Machine.code_at(root, fpath)
    except (IndexError, KeyError, AttributeError, TypeError) as exc:
        raise ImageFormatError(f"{path}.path: {exc}") from None
    if type(term).__name__ != term_tag:
        raise ImageFormatError(
            f"{path}: code coord gives {type(term).__name__}, "
            f"image claims {term_tag}"
        )

    k = _load_continuation(kobj, f"{path}.k", m, code, root)
    return Frame(
        record=record, code=code, fid=fid, version=version,
        term=term, path=list(fpath), k=k, kind=kind,
        count_steps=count_steps, call_entry=call_entry,
        versions_view=versions_view,
    )


def _load_continuation(kobj: dict, path: str, m: Machine, code: tuple,
                       root) -> dict:
    tag = kobj.get("k")
    if not isinstance(tag, str):
        raise ImageFormatError(f"{path}.k: string tag required")
    allowed = {
        "halt", "frame_done", "inits", "begin", "if_k", "prim",
        "call_func", "call_arg", "cellset", "fill", "mkcell", "clo_env",
    }
    if tag not in allowed:
        raise ImageFormatError(f"{path}: unknown continuation {tag!r}")
    k: dict = {"k": tag}

    if tag == "halt":
        return k
    if tag == "frame_done":
        return k

    # cellset / fill 用 depth/slot 定位；mkcell 无坐标；其余续延用 np 定位节点
    if tag in ("cellset", "fill", "mkcell"):
        if tag in ("cellset", "fill"):
            for key in ("depth", "slot"):
                if not isinstance(kobj.get(key), int) or isinstance(kobj[key], bool) \
                        or kobj[key] < 0:
                    raise ImageFormatError(f"{path}.{key}: non-negative int")
            name = kobj.get("name")
            if not isinstance(name, str):
                raise ImageFormatError(f"{path}.name: string")
            k["depth"] = kobj["depth"]
            k["slot"] = kobj["slot"]
            k["name"] = name
        k["inner"] = _inner(kobj, path, m, code, root)
        return k

    np = kobj.get("np")
    if not isinstance(np, list):
        raise ImageFormatError(f"{path}.np: path list required")
    try:
        node = Machine.code_at(root, np)
    except (IndexError, KeyError, AttributeError, TypeError):
        raise ImageFormatError(f"{path}.np: path out of range") from None
    if not isinstance(node, _NP_TYPES[tag]):
        raise ImageFormatError(
            f"{path}.np: expected {_NP_TYPES[tag].__name__}, "
            f"got {type(node).__name__}"
        )
    k["np"] = list(np)

    if tag in ("inits",):
        acc = kobj.get("acc")
        if not isinstance(acc, list):
            raise ImageFormatError(f"{path}.acc: list")
        for i, v in enumerate(acc):
            _validate_value(v, f"{path}.acc[{i}]")
        if len(acc) > len(node.inits):
            raise ImageFormatError(f"{path}: too many inits accumulated")
        schema = kobj.get("schema")
        if schema is not None and not isinstance(schema, str):
            raise ImageFormatError(f"{path}.schema: str|null")
        k["acc"] = list(acc)
        k["schema"] = schema
        return k
    if tag == "begin":
        i = _nonneg_int(kobj, "i", path)
        if i >= len(node.exprs):
            raise ImageFormatError(f"{path}.i: out of range")
        k["i"] = i
        k["inner"] = _inner(kobj, path, m, code, root)
        return k
    if tag == "if_k":
        k["inner"] = _inner(kobj, path, m, code, root)
        return k
    if tag == "prim":
        i = _nonneg_int(kobj, "i", path)
        acc = _int_value(kobj.get("acc"), f"{path}.acc")
        if i > len(node.args):
            raise ImageFormatError(f"{path}.i: out of range")
        k["i"] = i
        k["acc"] = acc
        k["inner"] = _inner(kobj, path, m, code, root)
        return k
    if tag == "call_func":
        k["inner"] = _inner(kobj, path, m, code, root)
        return k
    if tag == "call_arg":
        i = _nonneg_int(kobj, "i", path)
        if i >= len(node.args):
            raise ImageFormatError(f"{path}.i: arg index out of range")
        fn = kobj.get("fn")
        if not _is_ref(fn):
            raise ImageFormatError(f"{path}.fn: closure OID required")
        if fn not in m.heap.closures:
            raise ImageFormatError(f"{path}.fn: dangling closure {fn}")
        argv = kobj.get("argv")
        if not isinstance(argv, list) or len(argv) != i:
            raise ImageFormatError(f"{path}.argv: list of length i")
        for j, v in enumerate(argv):
            _validate_value(v, f"{path}.argv[{j}]")
        k["i"] = i
        k["fn"] = fn
        k["argv"] = list(argv)
        k["inner"] = _inner(kobj, path, m, code, root)
        return k
    if tag == "clo_env":
        i = _nonneg_int(kobj, "i", path)
        fid = kobj.get("fid")
        if not isinstance(fid, str):
            raise ImageFormatError(f"{path}.fid: string")
        if fid not in m.table.current:
            raise ImageFormatError(f"{path}.fid: unknown function {fid}")
        acc = kobj.get("acc")
        if not isinstance(acc, list) or len(acc) != i:
            raise ImageFormatError(f"{path}.acc: list of length i")
        for j, v in enumerate(acc):
            _validate_value(v, f"{path}.acc[{j}]")
        if i > len(node.env):
            raise ImageFormatError(f"{path}.i: env index out of range")
        if not isinstance(kobj.get("restore_count"), bool):
            raise ImageFormatError(f"{path}.restore_count: bool")
        k["i"] = i
        k["fid"] = fid
        k["acc"] = list(acc)
        k["restore_count"] = kobj["restore_count"]
        k["inner"] = _inner(kobj, path, m, code, root)
        return k
    raise ImageFormatError(f"{path}: unhandled continuation {tag}")  # pragma: no cover


def _inner(kobj: dict, path: str, m: Machine, code, root) -> dict:
    inner = kobj.get("inner")
    if not isinstance(inner, dict):
        raise ImageFormatError(f"{path}.inner: continuation required")
    return _load_continuation(inner, f"{path}.inner", m, code, root)


def _nonneg_int(kobj: dict, key: str, path: str) -> int:
    v = kobj.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ImageFormatError(f"{path}.{key}: non-negative int")
    return v


def _int_value(v, path: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise ImageFormatError(f"{path}: int expected")
    return v


def from_json(text: str) -> Machine:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImageFormatError(f"invalid JSON: {exc}") from None
    return load_machine(obj)


def load_image(path: str) -> Machine:
    with open(path, encoding="utf-8") as fh:
        return from_json(fh.read())

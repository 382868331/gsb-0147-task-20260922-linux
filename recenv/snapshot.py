"""快照序列化 / 恢复：跨进程保存暂停中的解释器。

格式为 ``recenv-snapshot-v1`` 的 JSON（仅标准库）。设计要点：

* **对象身份靠稳定 OID**：堆以 OID 为键序列化，槽位/记录/闭包互相以 OID
  引用。别名（共享 cell）、环（letrec rec-cell ↔ 闭包记录）按图原样恢复，
  绝不把别名复制成不同对象。序列化按 OID 升序写出，但反序列化两遍装载，
  结果与对象表排列次序、JSON 键顺序无关（有置换测试覆盖）。
* **引用图完整验证**：恢复时从全部根（控制环境、各续体帧的环境/中间值、
  result）做可达性遍历，任何悬空 OID / 类型不符都在发布机器前拒绝。
* **代码版本验证**：代码随快照保存（IR 的规范化 JSON + 内容哈希）。恢复时
  重算 SHA-256 与快照内哈希逐一核对——检查器不信任快照自带的“正确”标志。
* **续体表达式按内容哈希挂回**：每帧/控制位的 TExpr 存其节点内容哈希，
  恢复后映射回代码表中规范化注册的同一节点；哈希找不到即代码版本不一致，
  拒绝恢复。
"""

from __future__ import annotations

import dataclasses
import json

from . import ast_nodes as ast
from . import machine as mach
from .codebundle import (
    CodeBundle,
    CodeEntry,
    expr_hash,
    normalize_expr,
)
from .errors import SnapshotError
from .heap import CellObj, ClosureObj, Heap, RecordObj, Ref

SNAPSHOT_FORMAT = "recenv-snapshot-v1"
SNAPSHOT_VERSION = 1


# ---------------------------------------------------------------------------
# TExpr <-> JSON
# ---------------------------------------------------------------------------

def expr_to_json(e: object) -> object:
    return normalize_expr(e)


def expr_from_json(data: object) -> ast.TExpr:
    """把规范化 JSON 重建为 TExpr 节点树。"""
    return _rebuild(data)


def _rebuild(data):
    if data is None:
        return None
    tag = data[0]
    if tag == "int":
        return data[1]
    if tag == "bool":
        return data[1]
    if tag == "str":
        return data[1]
    if tag == "tuple":
        return tuple(_rebuild(x) for x in data[1])
    if tag == "node":
        cls_name = data[1]
        cls = _IR_CLASSES.get(cls_name)
        if cls is None:
            raise SnapshotError(f"unknown IR node type {cls_name!r}")
        kwargs = {fname: _rebuild(val) for fname, val in data[2]}
        return cls(**kwargs)
    raise SnapshotError(f"bad normalized tag {tag!r}")


_IR_CLASSES = {
    c.__name__: c
    for c in (
        ast.TConst, ast.TGet, ast.TCellGet, ast.TCellSet, ast.TPrim,
        ast.TMkClosure, ast.TMkCell, ast.TFillCell, ast.TCall, ast.TIf0,
        ast.TWithFrame, ast.TBegin,
    )
}


# ---------------------------------------------------------------------------
# 代码登记：节点内容哈希 -> 规范 TExpr 实例
# ---------------------------------------------------------------------------

def _walk_exprs(e, out: dict[str, object]) -> None:
    out[expr_hash(e)] = e
    for f in dataclasses.fields(e):
        v = getattr(e, f.name)
        if dataclasses.is_dataclass(v):
            _walk_exprs(v, out)
        elif isinstance(v, tuple):
            for x in v:
                if dataclasses.is_dataclass(x):
                    _walk_exprs(x, out)


class CodeIndex:
    """bundle 内全部 TExpr 节点的 内容哈希 -> 规范实例 索引。"""

    def __init__(self, bundle: CodeBundle) -> None:
        self.map: dict[str, object] = {}
        if bundle.main is not None:
            _walk_exprs(bundle.main, self.map)
        for entry in bundle.entries():
            _walk_exprs(entry.body, self.map)

    def key_of(self, e: object) -> str:
        return expr_hash(e)

    def get(self, key: str):
        e = self.map.get(key)
        if e is None:
            raise SnapshotError(
                "continuation references code not present in the version table "
                f"(hash {key[:12]}…); code version mismatch"
            )
        return e


# ---------------------------------------------------------------------------
# 槽值 / 堆
# ---------------------------------------------------------------------------

def _val_to_json(v) -> object:
    if isinstance(v, bool):
        return ["b", int(v)]
    if isinstance(v, int):
        return ["i", v]
    if isinstance(v, Ref):
        return ["r", v.oid]
    if v is None:
        return ["n"]
    raise SnapshotError(f"cannot serialize slot value {type(v).__name__}")


def _val_from_json(d) -> object:
    t = d[0]
    if t == "i":
        return int(d[1])
    if t == "b":
        return bool(d[1])
    if t == "r":
        return Ref(int(d[1]))
    if t == "n":
        return None
    raise SnapshotError(f"bad slot value tag {t!r}")


def heap_to_json(heap: Heap) -> dict:
    objs = {}
    for oid in heap.all_oids():
        obj = heap.get(Ref(oid))
        if isinstance(obj, CellObj):
            objs[str(oid)] = ["cell", obj.kind, bool(obj.filled),
                              _val_to_json(obj.value)]
        elif isinstance(obj, ClosureObj):
            objs[str(oid)] = [
                "closure", obj.fkey, obj.record.oid,
                obj.unit, obj.version, obj.member,
            ]
        elif isinstance(obj, RecordObj):
            objs[str(oid)] = [
                "record", obj.role,
                obj.parent.oid if obj.parent is not None else None,
                [_val_to_json(s) for s in obj.slots],
                obj.tag,
            ]
        else:  # pragma: no cover
            raise SnapshotError(f"cannot serialize heap object {type(obj).__name__}")
    return objs


def heap_from_json(data: dict, heap_limit: int) -> Heap:
    heap = Heap(limit=heap_limit)
    # 两遍：先全部装载对象（槽内引用暂不解析也无妨——Ref 只是整数对），
    # 再统一做引用图验证。这样装载顺序/键顺序不影响结果。
    for key, rec in data.items():
        try:
            oid = int(key)
        except ValueError:
            raise SnapshotError(f"bad heap object id {key!r}")
        kind = rec[0]
        if kind == "cell":
            _, ck, filled, val = rec
            if ck not in ("cell", "rec"):
                raise SnapshotError(f"bad cell kind {ck!r}")
            heap.load(oid, CellObj(oid, _val_from_json(val), bool(filled), ck))
        elif kind == "closure":
            _, fkey, rec_oid, unit, version, member = rec
            heap.load(oid, ClosureObj(
                oid, str(fkey), Ref(int(rec_oid)), unit, version, member,
            ))
        elif kind == "record":
            role = rec[1]
            parent = rec[2]
            slots = rec[3]
            tag = rec[4] if len(rec) > 4 else None
            heap.load(oid, RecordObj(
                oid,
                [_val_from_json(s) for s in slots],
                Ref(int(parent)) if parent is not None else None,
                str(role),
                tag,
            ))
        else:
            raise SnapshotError(f"bad heap object kind {kind!r}")
    return heap


# ---------------------------------------------------------------------------
# 代码（函数表）序列化
# ---------------------------------------------------------------------------

def bundle_to_json(bundle: CodeBundle) -> dict:
    funcs = {}
    for entry in bundle.entries():
        body_json = expr_to_json(entry.body)
        funcs[entry.fkey] = {
            "params": list(entry.params),
            "mutable_params": [bool(x) for x in entry.mutable_params],
            "free_names": list(entry.free_names),
            "capture_kinds": list(entry.capture_kinds),
            "unit": entry.unit,
            "version": entry.version,
            "member": entry.member,
            "body": body_json,
            "hash": entry.code_hash,
        }
    return {"main": expr_to_json(bundle.main), "functions": funcs}


def bundle_from_json(data: dict) -> tuple[CodeBundle, dict[str, str]]:
    bundle = CodeBundle()
    hashes: dict[str, str] = {}
    for fkey, fd in data["functions"].items():
        body = expr_from_json(fd["body"])
        actual_hash = expr_hash(body)
        # 检查器不信任快照自带哈希：重算并核对。
        if actual_hash != fd["hash"]:
            raise SnapshotError(
                f"code hash mismatch for {fkey!r}: snapshot claims "
                f"{fd['hash'][:12]}…, recomputed {actual_hash[:12]}…"
            )
        n_cap = len(fd["free_names"])
        entry = CodeEntry(
            fkey=fkey,
            params=tuple(fd["params"]),
            mutable_params=tuple(fd["mutable_params"]),
            body=body,
            free_names=tuple(fd["free_names"]),
            capture_kinds=tuple(fd["capture_kinds"]),
            unit=fd["unit"],
            version=fd["version"],
            member=fd["member"],
            code_hash=actual_hash,
        )
        if len(entry.capture_kinds) != n_cap:
            raise SnapshotError(f"capture_kinds length mismatch for {fkey!r}")
        bundle.add_entry(entry)
        hashes[fkey] = actual_hash
    bundle.main = expr_from_json(data["main"])
    return bundle, hashes


# ---------------------------------------------------------------------------
# 续体栈
# ---------------------------------------------------------------------------

_FRAME_TAGS = {
    "KPrim": mach.KPrim,
    "KCellSet": mach.KCellSet,
    "KMkCell": mach.KMkCell,
    "KFill": mach.KFill,
    "KClosure": mach.KClosure,
    "KCall": mach.KCall,
    "KIf": mach.KIf,
    "KWithFrame": mach.KWithFrame,
    "KBegin": mach.KBegin,
}


def _frame_to_json(k, index: CodeIndex) -> object:
    if isinstance(k, mach.KEnterCall):
        return ["enter", k.fkey,
                k.env.oid if k.env is not None else None,
                _val_to_json(k.callee), [_val_to_json(a) for a in k.args]]
    if isinstance(k, mach.KReturn):
        return ["ret", k.fkey, k.unit, k.version, k.closure_oid,
                k.caller_depth, k.bottom,
                index.key_of(k.saved_control) if k.saved_control is not None else None,
                k.saved_env.oid if k.saved_env is not None else None,
                k.saved_cur, k.saved_depth]
    tag = type(k).__name__
    env_oid = k.env.oid if k.env is not None else None
    base = [tag, k.fkey, env_oid, index.key_of(k.e)]
    if isinstance(k, mach.KPrim):
        base += [k.idx, [_val_to_json(v) for v in k.vals]]
    elif isinstance(k, mach.KClosure):
        base += [k.idx, [_val_to_json(v) for v in k.slots]]
    elif isinstance(k, mach.KWithFrame):
        base += [k.idx, [_val_to_json(v) for v in k.slots]]
    elif isinstance(k, mach.KCall):
        base += [k.phase, [_val_to_json(v) for v in k.vals]]
    elif isinstance(k, mach.KBegin):
        base += [k.idx]
    return base


def _frame_from_json(d, index):
    tag = d[0]
    if tag == "enter":
        env = Ref(int(d[2])) if d[2] is not None else None
        return mach.KEnterCall(
            fkey=d[1], env=env,
            callee=_val_from_json(d[3]),
            args=[_val_from_json(a) for a in d[4]],
        )
    if tag == "ret":
        saved_control = index.get(d[7]) if len(d) > 7 and d[7] is not None else None
        saved_env = Ref(int(d[8])) if len(d) > 8 and d[8] is not None else None
        saved_cur = d[9] if len(d) > 9 else None
        saved_depth = d[10] if len(d) > 10 else None
        return mach.KReturn(
            d[1], d[2], d[3], int(d[4]), int(d[5]), bool(d[6]),
            saved_control=saved_control, saved_env=saved_env,
            saved_cur=saved_cur, saved_depth=saved_depth,
        )
    cls = _FRAME_TAGS.get(tag)
    if cls is None:
        raise SnapshotError(f"unknown continuation frame {tag!r}")
    tag = d[0]
    fkey, env_oid, ekey = d[1], d[2], d[3]
    e = index.get(ekey)
    env = Ref(int(env_oid)) if env_oid is not None else None
    if tag in ("KPrim", "KClosure", "KWithFrame"):
        idx = int(d[4])
        vals = [_val_from_json(x) for x in d[5]]
        if tag == "KWithFrame":
            return cls(fkey=fkey, env=env, e=e, idx=idx, slots=vals)
        attr = "vals" if tag == "KPrim" else "slots"
        return cls(fkey=fkey, env=env, e=e, idx=idx, **{attr: vals})
    if tag == "KCall":
        return cls(fkey=fkey, env=env, e=e, phase=int(d[4]),
                   vals=[_val_from_json(x) for x in d[5]])
    if tag == "KBegin":
        return cls(fkey=fkey, env=env, e=e, idx=int(d[4]))
    return cls(fkey=fkey, env=env, e=e)


# ---------------------------------------------------------------------------
# 机器快照
# ---------------------------------------------------------------------------

def machine_to_snapshot(m: "mach.Machine") -> dict:
    """把暂停中的机器序列化为纯 JSON 字典（可再 json.dumps 跨进程传递）。"""
    if m.status != "paused":
        raise SnapshotError(
            f"snapshot is only valid at a paused safepoint, status={m.status!r}"
        )
    if m._pending is not None:
        raise SnapshotError("cannot snapshot while an upgrade is pending")
    index = CodeIndex(m.bundle)
    roots: list[Ref] = []
    frames_json = []
    for k in m.frames:
        frames_json.append(_frame_to_json(k, index))
        _collect_frame_roots(k, roots)
    if m.env is not None:
        roots.append(m.env)
    control_key = (
        "<call-entry>" if m.control is mach.CALL_ENTRY
        else index.key_of(m.control)
    )
    return {
        "format": SNAPSHOT_FORMAT,
        "version": SNAPSHOT_VERSION,
        "status": m.status,
        "limits": {
            "step": m.step_limit,
            "frame": m.frame_limit,
            "heap": m.heap.limit,
        },
        "steps": m.steps,
        "depth": m.depth,
        "events": m.events,
        "control": control_key,
        "cur_fkey": m.cur_fkey,
        "env": m.env.oid if m.env is not None else None,
        "frames": frames_json,
        "code": bundle_to_json(m.bundle),
        "heap": heap_to_json(m.heap),
        "applied_upgrades": [list(t) for t in m.applied_upgrades],
    }


def _collect_frame_roots(k, roots: list[Ref]) -> None:
    if isinstance(k, mach.KEnterCall):
        if k.env is not None:
            roots.append(k.env)
        if isinstance(k.callee, Ref):
            roots.append(k.callee)
        for a in k.args:
            if isinstance(a, Ref):
                roots.append(a)
        return
    if isinstance(k, mach.KReturn):
        if k.saved_env is not None:
            roots.append(k.saved_env)
        return
    if k.env is not None:
        roots.append(k.env)
    for attr in ("vals", "slots"):
        seq = getattr(k, attr, None)
        if seq:
            for v in seq:
                if isinstance(v, Ref):
                    roots.append(v)


def machine_from_snapshot(data: dict) -> "mach.Machine":
    """从快照完整验证并恢复机器；任何不一致都在返回前抛出，不留半成品。"""
    if not isinstance(data, dict) or data.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError("not a recenv snapshot (missing format marker)")
    if data.get("version") != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"unsupported snapshot version {data.get('version')!r}; "
            f"expected {SNAPSHOT_VERSION}"
        )
    if data.get("status") != "paused":
        raise SnapshotError("only paused snapshots can be resumed")

    limits = data["limits"]
    # 1) 代码：重算哈希验证，构建版本表与节点索引
    bundle, _ = bundle_from_json(data["code"])
    index = CodeIndex(bundle)

    # 2) 堆：两遍装载（此时不验证引用）
    heap = heap_from_json(data["heap"], int(limits["heap"]))

    # 3) 续体栈与控制位
    frames = [_frame_from_json(f, index) for f in data["frames"]]
    if data["control"] == "<call-entry>":
        control = mach.CALL_ENTRY
    else:
        control = index.get(data["control"])
    env = Ref(int(data["env"])) if data["env"] is not None else None

    # 控制表达式必须属于 cur_fkey 对应代码（main 或某函数体）——结构存在即可，
    # 因为 fkey 版本由各 KReturn 标记钉住；这里再核对 cur_fkey 已登记。
    if data["cur_fkey"] != "<main>" and not bundle.has(data["cur_fkey"]):
        raise SnapshotError(f"current function {data['cur_fkey']!r} not in code table")

    # 4) 引用图完整性：从全部根可达遍历，悬空引用在此拒绝
    roots: list[Ref] = []
    if env is not None:
        roots.append(env)
    for k in frames:
        _collect_frame_roots(k, roots)
    try:
        reachable = heap.reachable_from(roots)
    except KeyError as e:
        raise SnapshotError(f"dangling reference in snapshot heap: {e}") from e

    # 5) 逐对象语义校验：闭包 fkey 已登记且版本元数据一致；cell 引用合法
    for oid in reachable:
        obj = heap.get(Ref(oid))
        if isinstance(obj, ClosureObj):
            if not bundle.has(obj.fkey):
                raise SnapshotError(
                    f"closure #{oid} references unknown function {obj.fkey!r}"
                )
            entry = bundle.get(obj.fkey)
            rec = heap.record(obj.record)
            if len(rec.slots) != entry.n_capture:
                raise SnapshotError(
                    f"closure #{oid} ({entry.fkey}) capture record has "
                    f"{len(rec.slots)} slots, code expects {entry.n_capture}"
                )
            if entry.unit is not None:
                if obj.unit != entry.unit or obj.version != entry.version:
                    raise SnapshotError(
                        f"closure #{oid} version metadata "
                        f"{obj.unit}@{obj.version} disagrees with code "
                        f"{entry.unit}@{entry.version}"
                    )
        elif isinstance(obj, RecordObj):
            for s in obj.slots:
                if s is not None and not isinstance(s, (int, Ref)):
                    raise SnapshotError(f"illegal slot content in record #{oid}")
        elif isinstance(obj, CellObj):
            if obj.value is not None and not isinstance(obj.value, (int, Ref)):
                raise SnapshotError(f"illegal cell content in cell #{oid}")

    # 6) 续体帧结构校验：KReturn 嵌套/深度自洽；KCall phase 范围等
    _verify_frames(frames, bundle, heap)

    # 7) 事件必须是合法形状（恢复后继续 append，序列化结果要可重复）
    events = [tuple(ev) for ev in data["events"]]
    for ev in events:
        if (len(ev) != 3 or ev[0] != "set"
                or not isinstance(ev[1], str)
                or isinstance(ev[2], bool) or not isinstance(ev[2], int)):
            raise SnapshotError(f"illegal event record {ev!r}")

    # 8) 全部验证通过后才构造并发布机器
    m = mach.Machine(
        bundle=bundle, heap=heap,
        step_limit=int(limits["step"]),
        frame_limit=int(limits["frame"]),
        heap_limit=int(limits["heap"]),
    )
    m.frames = frames
    m.control = control
    m.env = env
    m.cur_fkey = data["cur_fkey"]
    m.depth = int(data["depth"])
    m.steps = int(data["steps"])
    m.events = [tuple(ev) for ev in events]
    m.status = "paused"
    m.applied_upgrades = [tuple(t) for t in data.get("applied_upgrades", [])]
    # 段预算是恢复后的新选择，不沿用旧段；暂停标志重置。
    m._seg_budget = None
    m._seg_used = 0
    m._pause_wanted = False
    return m


def _verify_frames(frames: list, bundle: CodeBundle, heap: Heap) -> None:
    n_bottom = 0
    for k in frames:
        if isinstance(k, mach.KReturn):
            if k.bottom:
                n_bottom += 1
            elif not bundle.has(k.fkey):
                raise SnapshotError(
                    f"active frame references unknown function {k.fkey!r}"
                )
            elif not k.bottom:
                entry = bundle.get(k.fkey)
                if (entry.unit, entry.version) != (k.unit, k.version):
                    raise SnapshotError(
                        f"frame {k.fkey!r} pinned version {k.unit}@{k.version} "
                        f"disagrees with code {entry.unit}@{entry.version}"
                    )
        elif isinstance(k, mach.KCall):
            if not (0 <= k.phase <= len(k.e.args) + 1):
                raise SnapshotError("KCall phase out of range")
            if len(k.vals) != k.phase:
                raise SnapshotError("KCall partial values disagree with phase")
        elif isinstance(k, (mach.KPrim,)):
            if not (0 <= k.idx <= len(k.e.args)) or len(k.vals) != k.idx:
                raise SnapshotError("KPrim continuation inconsistent")
        elif isinstance(k, (mach.KClosure,)):
            if not (0 <= k.idx <= len(k.e.env)) or len(k.slots) != k.idx:
                raise SnapshotError("KClosure continuation inconsistent")
        elif isinstance(k, mach.KWithFrame):
            if not (0 <= k.idx <= len(k.e.inits)) or len(k.slots) != k.idx:
                raise SnapshotError("KWithFrame continuation inconsistent")
        elif isinstance(k, mach.KBegin):
            if not (0 <= k.idx < len(k.e.exprs)):
                raise SnapshotError("KBegin continuation inconsistent")
        elif isinstance(k, mach.KEnterCall):
            if not isinstance(k.callee, Ref):
                raise SnapshotError("KEnterCall callee must be a heap reference")
            clo = heap.get(k.callee)
            if not isinstance(clo, ClosureObj) or not bundle.has(clo.fkey):
                raise SnapshotError("KEnterCall callee is not a registered closure")
            if len(k.args) != bundle.get(clo.fkey).arity:
                raise SnapshotError("KEnterCall arity inconsistent with code")
            for a in k.args:
                if a is not None and not isinstance(a, (int, Ref)):
                    raise SnapshotError("KEnterCall illegal argument")
    if n_bottom != 1:
        raise SnapshotError(
            f"snapshot must contain exactly one bottom call marker, found {n_bottom}"
        )


# ---------------------------------------------------------------------------
# 便利函数
# ---------------------------------------------------------------------------

def dumps(m: "mach.Machine", *, indent: int | None = None) -> str:
    return json.dumps(machine_to_snapshot(m), ensure_ascii=False, indent=indent,
                      sort_keys=False)


def loads(text: str) -> "mach.Machine":
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SnapshotError(f"malformed snapshot JSON: {e}") from e
    return machine_from_snapshot(data)


def dump_bytes(m: "mach.Machine") -> bytes:
    return dumps(m).encode("utf-8")


def load_bytes(b: bytes) -> "mach.Machine":
    return loads(b.decode("utf-8"))

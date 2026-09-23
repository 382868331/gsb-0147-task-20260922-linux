"""显式字段迁移 + 安全点热升级（全有或全无）。

版本化单元（独立带标签函数或带标签互递归组）可以从版本 v 升级到 v+1。
新版本代码由一个新的 :class:`~recenv.ast_nodes.Program` 给出（经 convert，
函数带 ``name@v+1`` / 组标签），旧实例按显式规则迁移其捕获环境：

* **重命名** ``rename``: {旧字段名: 新字段名}；
* **删除未被引用字段** ``delete``: [字段名 …]，只能删除新版本不再引用的字段；
* **增加带常量默认值字段** ``add``: {新字段名: 整数默认值}（常量必须是
  Python ``int`` 且非 bool；字段种类固定为不可变 ``"val"``）。

规则之外的任何字段集差异都拒绝。流程严格分阶段，任一阶段失败都不改变
运行机器：

1. 登记新代码（合并进 bundle 的*影子*副本），校验后继版本关系；
2. 由规则计算新旧字段映射并静态验证（默认值常量化、删除字段无新引用、
   重命名不冲突、字段集恰好闭合）；
3. 扫描旧堆，找出该单元在 v 版的全部存活实例；
4. 在影子堆对象上构造新捕获记录与新闭包（旧对象不动）；
5. 引用图与版本复核；
6. **提交**：把新代码并入机器 bundle、新对象并入机器堆，再把所有指向旧闭包
   的槽位/cell 原子改指向新闭包。提交前的注入失败点（failpoint）触发时抛出
   :class:`~recenv.errors.MigrationError` 并完整回滚——机器与升级前逐位等价。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ast_nodes as ast
from .codebundle import CodeBundle, CodeEntry
from .errors import (
    CodeVersionError,
    MigrationError,
    SnapshotError,
)
from .heap import CellObj, ClosureObj, RecordObj, Ref

# 注入失败点（测试用）：在提交的各阶段之前抛出，验证完整回滚。
FAILPOINTS = (
    "before_register", "after_shadow_build", "before_commit_code",
    "before_commit_objects", "before_rewire", "after_rewire",
)


@dataclass(frozen=True)
class MigrationRules:
    rename: dict[str, str] = field(default_factory=dict)
    delete: tuple[str, ...] = ()
    add: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for k, v in self.rename.items():
            if not isinstance(k, str) or not isinstance(v, str) or not k or not v:
                raise MigrationError("rename rules must map non-empty field names")
        for d in self.delete:
            if not isinstance(d, str) or not d:
                raise MigrationError("delete rules must name non-empty fields")
        for k, v in self.add.items():
            if not isinstance(k, str) or not k:
                raise MigrationError("add rules must name non-empty fields")
            if isinstance(v, bool) or not isinstance(v, int):
                raise MigrationError(
                    f"added field {k!r} default must be a constant int, "
                    f"got {type(v).__name__}"
                )
        # 规则内部一致性
        if set(self.rename) & set(self.delete):
            raise MigrationError("a field cannot be both renamed and deleted")
        if set(self.rename.values()) & set(self.add):
            raise MigrationError("rename target collides with an added field")
        if set(self.delete) & set(self.add):
            raise MigrationError("a field cannot be both deleted and added")


@dataclass
class _FieldMap:
    """旧字段槽 -> 新字段槽 的逐成员映射。"""

    # 成员名 -> {"new_external": [新字段名顺序], "mapping": [(新槽索引, 旧槽索引|('add',默认值)|('rename',旧槽索引))]}
    members: dict[str, dict] = field(default_factory=dict)


def _external_indices(entry: CodeEntry) -> dict[str, int]:
    """字段名 -> 在*外部字段序列*（非 rec 槽）中的索引。"""
    out: dict[str, int] = {}
    j = 0
    for name, kind in zip(entry.free_names, entry.capture_kinds):
        if kind != "rec":
            out[name] = j
            j += 1
    return out


def build_field_map(
    old_members: dict[str, CodeEntry],
    new_members: dict[str, CodeEntry],
    rules: MigrationRules,
) -> _FieldMap:
    """静态计算字段映射并验证规则恰好闭合字段集差异。

    规则是单元级的，但每个成员捕获的字段子集可以不同：规则对某成员的适用性
    取决于该成员是否拥有该字段；单元级约束（见下）保证没有字段被静默忽略。
    """
    fmap = _FieldMap()

    # 单元级字段集合
    old_all = {f.name for mm in old_members.values() for f in mm.external_fields()}
    new_all = {f.name for mm in new_members.values() for f in mm.external_fields()}

    # 单元级静态约束
    for d in rules.delete:
        if d not in old_all:
            raise MigrationError(
                f"delete target {d!r} is not a field of any old member"
            )
        if d in new_all:
            raise MigrationError(
                f"cannot delete field {d!r}: new version still references it"
            )
    for a in rules.add:
        if a in old_all:
            raise MigrationError(f"added field {a!r} already exists in old version")
        if a not in new_all:
            raise MigrationError(
                f"added field {a!r} is not captured by any new member"
            )
    for src, dst in rules.rename.items():
        if src not in old_all:
            raise MigrationError(
                f"rename source {src!r} is not a field of any old member"
            )
        if dst in old_all and dst not in rules.rename.values():
            # dst 已是别的旧字段名：会冲突
            raise MigrationError(f"rename target {dst!r} already names a field")

    for mname in old_members:
        old = old_members[mname]
        new = new_members[mname]
        old_fields = old.external_fields()
        new_fields = new.external_fields()
        old_names = [f.name for f in old_fields]
        new_names = [f.name for f in new_fields]
        old_set, new_set = set(old_names), set(new_names)

        # 对本成员适用的规则子集
        rename_here = {s: d for s, d in rules.rename.items() if s in old_set}
        delete_here = tuple(d for d in rules.delete if d in old_set)
        add_here = {a: v for a, v in rules.add.items() if a in new_set}

        # 新成员出现某重命名目标但旧成员缺源：字段集无法闭合
        for src, dst in rules.rename.items():
            if dst in new_set and src not in old_set:
                raise MigrationError(
                    f"rename {src!r} -> {dst!r}: new member {mname!r} has "
                    f"{dst!r} but old member lacks {src!r}"
                )

        # 字段集必须恰好被规则闭合
        moved = set(rename_here) | set(delete_here)
        surviving = [n for n in old_names if n not in moved]
        expected_new = sorted(
            surviving + list(rename_here.values()) + list(add_here)
        )
        if sorted(new_names) != expected_new:
            raise MigrationError(
                f"field sets do not close under the given rules for {mname!r}: "
                f"rules imply {expected_new}, new code has {sorted(new_names)}"
            )

        # 种类兼容：保留/重命名的字段在新版本必须种类一致
        old_by_name = {f.name: f for f in old_fields}
        new_by_name = {f.name: f for f in new_fields}
        carry: dict[str, str] = {}  # 新名 -> 旧名
        for n in surviving:
            carry[n] = n
        for src, dst in rename_here.items():
            carry[dst] = src
        for new_name, old_name in carry.items():
            if old_by_name[old_name].kind != new_by_name[new_name].kind:
                ok, nk = old_by_name[old_name].kind, new_by_name[new_name].kind
                raise MigrationError(
                    f"field {old_name!r}->{new_name!r} changed kind {ok!r}->{nk!r}; "
                    f"only rename/delete/add-default are permitted"
                )

        # 构造外部槽映射：新外部槽 j 的来源
        old_ext_idx = _external_indices(old)
        plan: list = []
        for nf in new_fields:
            if nf.name in add_here:
                plan.append(("add", add_here[nf.name]))
            else:
                plan.append(("copy", old_ext_idx[carry[nf.name]]))
        fmap.members[mname] = {
            "new_external": new_names,
            "plan": plan,
            # rec 槽（伙伴函数）在新捕获记录中的位置：按新 free_names
            "rec_new": [i for i, (n, k) in enumerate(
                zip(new.free_names, new.capture_kinds)) if k == "rec"],
            "rec_old": [i for i, (n, k) in enumerate(
                zip(old.free_names, old.capture_kinds)) if k == "rec"],
            "rec_names_new": [n for n, k in zip(
                new.free_names, new.capture_kinds) if k == "rec"],
            "rec_names_old": [n for n, k in zip(
                old.free_names, old.capture_kinds) if k == "rec"],
            "new_entry": new,
            "old_entry": old,
        }
    return fmap


# ---------------------------------------------------------------------------
# 升级主流程
# ---------------------------------------------------------------------------

def apply_upgrade(
    machine,
    unit: str,
    new_program: ast.Program,
    rules: MigrationRules,
    *,
    failpoint: str | None = None,
    failhook=None,
) -> list[dict[str, Ref]]:
    """在安全点对 ``unit`` 应用升级。成功返回每实例的新句柄（成员名->Ref）。

    失败时机器不发生任何可观察变化（完整回滚）。
    """
    if failpoint is not None and failpoint not in FAILPOINTS:
        raise MigrationError(f"unknown failpoint {failpoint!r}")

    def _fire(name: str) -> None:
        if failpoint == name:
            if failhook is not None:
                failhook(name)
            raise MigrationError(f"injected failure at {name!r}")

    # --- 阶段 0：定位旧版本 ---------------------------------------------
    if unit not in machine.bundle.all_units():
        raise CodeVersionError(f"unknown versioned unit {unit!r}")
    old_version = machine.bundle.latest(unit)
    new_version = old_version + 1
    old_members = machine.bundle.members(unit, old_version)

    # --- 阶段 1：影子登记新代码 ------------------------------------------
    _fire("before_register")
    shadow_bundle = _clone_bundle(machine.bundle)
    # 新 Program 整体以唯一前缀并入影子表：其内部辅助函数一并可被新版本调用，
    # 且 fkey 与在途旧代码不冲突。
    prefix = f"u:{unit}@{new_version}:"
    remap = shadow_bundle.import_program(new_program, prefix)
    new_members = shadow_bundle.members(unit, new_version)
    imported_fkeys = set(remap.values())
    # 后继关系（成员集合/元数/参数可变性）
    for m in old_members:
        shadow_bundle.check_successor(old_members[m], new_members[m])

    # --- 阶段 2：字段映射静态验证 ----------------------------------------
    fmap = build_field_map(old_members, new_members, rules)

    # --- 阶段 3：扫描旧实例 ----------------------------------------------
    # 实例分组：同一 letrec 组的成员闭包构成一个实例。识别方式：组实例的成员
    # 闭包经彼此 rec-cell 可达；独立函数每个闭包自成一个实例。
    instances = _collect_instances(machine.heap, machine.bundle, unit, old_version)

    # --- 阶段 4：影子构造新记录/新闭包（不动机器堆） ---------------------
    # new_objects: [(type, kwargs)]；rewire 计划延后到提交阶段生成。
    shadow: list[dict] = []
    handles: list[dict[str, Ref]] = []
    # 为新对象预分配 OID（在影子空间内逻辑分配，提交时写入机器堆）
    next_oid = max(machine.heap.all_oids(), default=0) + 1

    def _alloc() -> int:
        nonlocal next_oid
        oid = next_oid
        next_oid += 1
        return oid

    # 新版本中作为 rec 伙伴被引用的成员集合（独立函数无 rec 槽）
    rec_member_names: set[str] = set()
    for mm in new_members.values():
        for n, k in zip(mm.free_names, mm.capture_kinds):
            if k == "rec":
                rec_member_names.add(n)

    # 先为每个实例的每个成员分配新闭包 OID；被伙伴引用的成员再分配一个新
    # rec-cell OID（伙伴槽经 rec-cell 引用）。旧组 rec-cell 不动，在途旧栈帧
    # 继续经它调用旧版本伙伴。
    for inst in instances:
        new_closure_oids: dict[str, int] = {}
        new_cell_oids: dict[str, int] = {}
        for mname in old_members:
            new_closure_oids[mname] = _alloc()
            if mname in rec_member_names:
                new_cell_oids[mname] = _alloc()
        inst["new_closure_oids"] = new_closure_oids
        inst["new_cell_oids"] = new_cell_oids

    for inst in instances:
        member_handles: dict[str, Ref] = {}
        new_records: dict[str, int] = {}
        for mname, old_entry in old_members.items():
            new_entry = new_members[mname]
            spec = fmap.members[mname]
            old_cap = machine.heap.record(inst["closures"][mname].record).slots
            # 外部字段（非 rec）在*完整捕获槽*中的位置，按外部顺序
            old_ext_full = [
                i for i, k in enumerate(old_entry.capture_kinds) if k != "rec"
            ]
            new_slots: list = []
            ext_iter = iter(spec["plan"])
            for fname, kind in zip(new_entry.free_names, new_entry.capture_kinds):
                if kind == "rec":
                    # 伙伴槽 -> 该伙伴的新 rec-cell（cell.value 为伙伴新闭包）
                    new_slots.append(Ref(inst["new_cell_oids"][fname]))
                else:
                    src = next(ext_iter)
                    if src[0] == "add":
                        new_slots.append(src[1])
                    else:
                        new_slots.append(old_cap[old_ext_full[src[1]]])
            rec_oid = _alloc()
            new_records[mname] = rec_oid
            shadow.append({"type": "record", "oid": rec_oid, "slots": new_slots})
        # 新 rec-cell：kind="rec"，已填充为对应成员的新闭包
        for mname in inst["new_cell_oids"]:
            shadow.append({
                "type": "rec_cell",
                "oid": inst["new_cell_oids"][mname],
                "value": Ref(inst["new_closure_oids"][mname]),
            })
        for mname, old_entry in old_members.items():
            new_entry = new_members[mname]
            oid = inst["new_closure_oids"][mname]
            shadow.append({
                "type": "closure", "oid": oid,
                "fkey": new_entry.fkey, "record": Ref(new_records[mname]),
                "unit": unit, "version": new_version, "member": mname,
            })
            member_handles[mname] = Ref(oid)
        inst["new_records"] = new_records
        handles.append(member_handles)
    _fire("after_shadow_build")

    # --- 阶段 5：影子图校验（新对象引用闭合、cell 共享保持） --------------
    _verify_shadow(shadow, new_members, fmap, instances, machine.heap)

    # 实例身份稳定性：新实例数必须等于旧实例数，逐成员一一对应
    if len(handles) != len(instances):  # pragma: no cover - 构造即保证
        raise MigrationError("instance count changed during migration")

    # --- 阶段 6：提交（之后才改机器）。全部变更可回滚。 ------------------
    _fire("before_commit_code")
    # 影子表中导入的条目即最终条目（fkey 已定），直接并入机器代码表。
    committed_entries = [shadow_bundle.get(k) for k in sorted(imported_fkeys)]

    added_fkeys: list[str] = []
    added_oids: list[int] = []
    rewind: list = []
    committed = False
    try:
        _fire("before_commit_objects")
        # 6a) 合并新代码
        for entry in committed_entries:
            machine.bundle.add_entry(entry)
            added_fkeys.append(entry.fkey)

        # 6b) 装载新对象
        for spec_obj in shadow:
            if spec_obj["type"] == "record":
                machine.heap.load(spec_obj["oid"], RecordObj(
                    oid=spec_obj["oid"], slots=list(spec_obj["slots"]),
                    parent=None, role="capture",
                ))
            elif spec_obj["type"] == "rec_cell":
                machine.heap.load(spec_obj["oid"], CellObj(
                    oid=spec_obj["oid"], value=spec_obj["value"],
                    filled=True, kind="rec",
                ))
            else:
                machine.heap.load(spec_obj["oid"], ClosureObj(
                    oid=spec_obj["oid"], fkey=spec_obj["fkey"],
                    record=spec_obj["record"], unit=spec_obj["unit"],
                    version=spec_obj["version"], member=spec_obj["member"],
                ))
            added_oids.append(spec_obj["oid"])

        # 6c) 改线。区分两类对旧成员闭包的引用：
        #     - 成员回边（成员捕获记录 / 在途成员激活帧中，位于该函数 rec 槽）：
        #       保持指向旧组 rec-cell，旧版本递归组因此自洽地继续跑旧代码；
        #     - 外部持有者（drive 等非成员函数的捕获记录/激活帧、独立版本函数
        #       的直接捕获）：改指向新版本。经 cell 间接持有的，替换为每实例每
        #       成员一个的新“外部 cell”（共享 X 的多个持有者仍共享同一新 cell，
        #       别名不复制）。
        _fire("before_rewire")
        owner: dict[int, tuple[int, str]] = {}
        for idx, inst in enumerate(instances):
            for mname, clo in inst["closures"].items():
                owner[clo.oid] = (idx, mname)

        old_fkeys = {clo.fkey for inst in instances for clo in inst["closures"].values()}
        rec_index_by_fkey = {}
        for fkey in old_fkeys:
            e = machine.bundle.get(fkey)
            rec_index_by_fkey[fkey] = {
                i for i, k in enumerate(e.capture_kinds) if k == "rec"
            }

        # 受保护槽位：成员捕获记录的 rec 槽 + 在途成员激活帧的 rec 槽。
        # 激活帧由 RecordObj(tag=fkey) 辨认（fkey 属于本单元旧版本成员函数）。
        protected: set[tuple[int, int]] = set()
        for idx, inst in enumerate(instances):
            for mname, clo in inst["closures"].items():
                cap = machine.heap.record(clo.record)
                for i in rec_index_by_fkey.get(clo.fkey, ()):
                    protected.add((cap.oid, i))
        for oid in machine.heap.all_oids():
            obj = machine.heap.get(Ref(oid))
            if isinstance(obj, RecordObj) and obj.role == "activation" \
                    and obj.tag in old_fkeys:
                for i in rec_index_by_fkey.get(obj.tag, ()):
                    protected.add((oid, i))

        # 每实例每成员一个的外部 cell（惰性分配），值为对应新闭包
        ext_cell: dict[tuple[int, str], int] = {}

        def _external_cell(idx: int, mname: str) -> Ref:
            key = (idx, mname)
            if key not in ext_cell:
                oid = _alloc()
                ext_cell[key] = oid
                shadow.append({
                    "type": "ext_cell", "oid": oid,
                    "value": Ref(instances[idx]["new_closure_oids"][mname]),
                })
            return Ref(ext_cell[key])

        def _redirect_slot(record_oid: int, i: int, v):
            if not isinstance(v, Ref) or (record_oid, i) in protected:
                return v
            target = machine.heap.get(v)
            # 直接持有旧成员闭包（独立版本函数的 val 捕获）
            if isinstance(target, ClosureObj) and v.oid in owner:
                idx, mname = owner[v.oid]
                return Ref(instances[idx]["new_closure_oids"][mname])
            # 经 cell 持有旧成员闭包（外部持有者）：换成共享的新外部 cell
            if isinstance(target, CellObj) and isinstance(target.value, Ref) \
                    and target.value.oid in owner:
                idx, mname = owner[target.value.oid]
                return _external_cell(idx, mname)
            return v

        # 先提交改线（可能惰性产生新 ext_cell，需在扫描后统一装载），记录回滚
        for oid in machine.heap.all_oids():
            obj = machine.heap.get(Ref(oid))
            if isinstance(obj, RecordObj):
                for i, s in enumerate(obj.slots):
                    ns = _redirect_slot(oid, i, s)
                    if ns is not s:
                        rewind.append(("slot", oid, i, s))
                        obj.slots[i] = ns
        # 装载改线阶段惰性产生的外部 cell（含回滚簿记）
        for spec_obj in shadow:
            if spec_obj["type"] == "ext_cell" and spec_obj["oid"] not in added_oids:
                machine.heap.load(spec_obj["oid"], CellObj(
                    oid=spec_obj["oid"], value=spec_obj["value"],
                    filled=True, kind="cell",
                ))
                added_oids.append(spec_obj["oid"])
        _fire("after_rewire")
        committed = True
    except BaseException:
        _rollback(machine, added_fkeys, added_oids, rewind)
        raise

    if not committed:  # pragma: no cover - 防御
        _rollback(machine, added_fkeys, added_oids, rewind)
        raise MigrationError("upgrade commit failed and was rolled back")

    machine.applied_upgrades.append((unit, old_version, new_version))
    return handles


def _rollback(machine, added_fkeys, added_oids, rewind) -> None:
    """逆序撤销提交：先恢复改线，再卸载新对象、撤销新代码。"""
    for change in reversed(rewind):
        kind, oid, i, old = change
        obj = machine.heap.get(Ref(oid))
        if kind == "slot":
            obj.slots[i] = old
        else:
            obj.value = old
    for oid in added_oids:
        machine.heap.unload(oid)
    for fkey in reversed(added_fkeys):
        machine.bundle._remove_entry(fkey)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _clone_bundle(bundle: CodeBundle) -> CodeBundle:
    """浅克隆代码表（条目不可变；只复制索引字典）。"""
    b = CodeBundle()
    for entry in bundle.entries():
        b.add_entry(entry)
    b.main = bundle.main
    return b


def _collect_instances(
    heap: "object", bundle: CodeBundle, unit: str, version: int
) -> list[dict]:
    """找出堆上该单元指定版本的全部存活实例。

    组实例：从任一旧成员闭包出发，经捕获记录的 rec 槽闭包集合得到完整成员集；
    独立版本函数：每个闭包一个实例（成员名 ``$self``）。
    """
    member_fkeys = {
        m: e.fkey for m, e in bundle.members(unit, version).items()
    }
    fkey_to_member = {f: m for m, f in member_fkeys.items()}
    found: dict[int, dict] = {}

    for oid in heap.all_oids():
        obj = heap.get(Ref(oid))
        if (isinstance(obj, ClosureObj) and obj.unit == unit
                and obj.version == version):
            found[oid] = obj

    grouped: list[dict] = {}
    visited: set[int] = set()
    instances: list[dict] = []
    for oid, clo in found.items():
        if oid in visited:
            continue
        closures: dict[str, ClosureObj] = {}
        stack = [clo]
        while stack:
            c = stack.pop()
            if c.oid in visited:
                continue
            visited.add(c.oid)
            mname = fkey_to_member.get(c.fkey)
            if mname is None:
                continue
            closures[mname] = c
            entry = bundle.get(c.fkey)
            rec = heap.record(c.record)
            # rec 伙伴槽装的是组帧的 rec-cell，cell.value 才是伙伴闭包；
            # 其余槽（cell/val）不沿用作组内遍历。
            for pos, kind in enumerate(entry.capture_kinds):
                slot = rec.slots[pos]
                if kind != "rec" or not isinstance(slot, Ref):
                    continue
                target = heap.get(slot)
                if isinstance(target, CellObj):
                    target = heap.get(target.value) if isinstance(target.value, Ref) else None
                if (isinstance(target, ClosureObj) and target.unit == unit
                        and target.version == version):
                    stack.append(target)
        missing = set(member_fkeys) - set(closures)
        if missing:
            raise SnapshotError(
                f"dangling migration group: unit {unit!r}@{version} instance "
                f"missing members {sorted(missing)}"
            )
        instances.append({"closures": closures})
    return instances


def _verify_shadow(shadow, new_members, fmap, instances, heap) -> None:
    """提交前对新对象做内部一致性校验。"""
    by_oid = {s["oid"]: s for s in shadow}
    new_closure_oids = {
        s["oid"] for s in shadow if s["type"] == "closure"
    }
    new_cell_oids = {
        s["oid"] for s in shadow if s["type"] == "rec_cell"
    }
    for s in shadow:
        if s["type"] == "closure":
            rec_spec = by_oid[s["record"].oid]
            entry = new_members[s["member"]]
            if len(rec_spec["slots"]) != entry.n_capture:
                raise MigrationError(
                    f"new capture record for {s['member']!r} has "
                    f"{len(rec_spec['slots'])} slots, expected {entry.n_capture}"
                )
            # rec 槽必须指向一个新 rec-cell，且该 cell 填充的是同实例新成员闭包
            for pos, (n, k) in enumerate(
                zip(entry.free_names, entry.capture_kinds)
            ):
                slot = rec_spec["slots"][pos]
                if k == "rec":
                    if not (isinstance(slot, Ref) and slot.oid in new_cell_oids):
                        raise MigrationError(
                            "rec slot not rewired to a new group rec-cell"
                        )
                    filled = by_oid[slot.oid]["value"]
                    if not (isinstance(filled, Ref)
                            and filled.oid in new_closure_oids):
                        raise MigrationError(
                            "new rec-cell not filled with a new group closure"
                        )

    # 共享对象身份：旧捕获记录中同一个 OID 被多个槽持有时，迁移后映射到的
    # 新 OID 集合必须仍只有一个——不能把别名复制成不同对象。
    for inst in instances:
        migrated_targets: dict[int, set] = {}
        for mname in inst["closures"]:
            old_entry = fmap.members[mname]["old_entry"]
            old_slots = heap.record(inst["closures"][mname].record).slots
            new_slots = by_oid[inst["new_records"][mname]]["slots"]
            old_ext_full = [
                i for i, k in enumerate(old_entry.capture_kinds) if k != "rec"
            ]
            new_entry = fmap.members[mname]["new_entry"]
            new_ext_full = [
                i for i, k in enumerate(new_entry.capture_kinds) if k != "rec"
            ]
            for j, full_new in enumerate(new_ext_full):
                plan_item = fmap.members[mname]["plan"][j]
                if plan_item[0] != "copy":
                    continue
                nv = new_slots[full_new]
                old_oid = old_slots[old_ext_full[plan_item[1]]]
                if isinstance(old_oid, Ref) and isinstance(nv, Ref):
                    migrated_targets.setdefault(old_oid.oid, set()).add(nv.oid)
        for old_oid, new_oids in migrated_targets.items():
            if len(new_oids) > 1:
                raise MigrationError(
                    f"shared object #{old_oid} was duplicated during migration "
                    f"({sorted(new_oids)})"
                )

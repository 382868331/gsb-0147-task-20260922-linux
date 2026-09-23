"""显式栈归约机：不依赖 Python 递归栈保存运行态。

与旧的递归下降目标解释器（:mod:`recenv.target_interp`，保留为独立参考）
执行同一份 IR、同一步数口径，但运行态全部是堆（:mod:`recenv.heap`）上的
OID 加一个*显式控制栈*，因此：

* 可在任意归约边界暂停（``run(step_budget=...)`` 到期即停在“下一条待归约
  表达式”之前，调用栈中间也可以），状态可序列化、跨进程恢复；
* **安全点** = 调用入口（激活帧刚压入、函数体尚未归约）。版本升级只允许
  在安全点进行；栈中旧帧保留自己的 ``(fid, version)`` 坐标，继续执行旧版
  本体；新调用经闭包载荷中的版本号解析到新版本；
* 每个栈帧的当前表达式带*代码坐标路径*，恢复时按 ``(fid, version)``
  取回代码体并逐结构核对，不执行镜像里夹带的代码副本。

帧模型
------

* 主帧（kind="main", record=None, k=halt）；
* 每个 ``TWithFrame`` 压入一个*控制帧*（kind="frame"）：init 求值期间
  record 为 None（不出现在环境链中），init 全部在外层链求值完才挂入新
  记录，然后求 body；
* 每次调用压入一个*激活帧*（kind="act"，record 已挂入），body 归约完出帧。
函数激活帧在链顶把捕获槽+参数槽扁平化，与旧解释器单帧激活语义一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import ast_nodes as ast
from .codetable import CodeTable
from .errors import LangRuntimeError, MachineStateError, StepLimitError
from .heap import Heap

MAIN_CODE = ("main",)

_COUNTED = (
    ast.TConst, ast.TGet, ast.TCellGet, ast.TCellSet, ast.TPrim,
    ast.TMkClosure, ast.TCall, ast.TIf0, ast.TWithFrame,
)


@dataclass
class Frame:
    record: str | None          # 堆记录 OID；None = 主帧或 init 未完成的控制帧
    code: tuple                 # ("main",) | ("fn", fid, version)
    fid: str | None
    version: str | None
    term: ast.TExpr             # 下一条待归约表达式
    path: list                  # term 在 code 代码根中的坐标
    k: dict                     # 续延（见 _deliver 中的各 tag）
    kind: str = "frame"         # main | frame | act
    count_steps: bool = True    # TMkClosure 抽取环境槽期间为 False
    call_entry: bool = False    # 安全点：调用入口、函数体尚未归约
    # 构造新闭包时解析 fid -> version 的*冻结视图*：
    # 主帧=程序初始版本；控制帧=发起它的帧视图；激活帧=调用时刻的 current
    # 快照。于是旧版本栈帧在函数体内构造的闭包仍按旧版本绑定，
    # 升级后新压入的激活帧才解析到新版本——环境引用不会跨版本混淆。
    versions_view: dict | None = None


class Machine:
    """可暂停 / 恢复 / 序列化 / 升级的栈式执行机。"""

    def __init__(
        self,
        program: ast.Program,
        step_limit: int = 10_000,
        heap: Heap | None = None,
        table: CodeTable | None = None,
        events: list | None = None,
        steps: int = 0,
    ) -> None:
        self.program = program
        self.table = table if table is not None else CodeTable(program)
        self.heap = heap if heap is not None else Heap()
        self.step_limit = step_limit
        self.steps = steps
        self.events: list[tuple[str, str, int]] = list(events) if events else []
        self.stack: list[Frame] = [
            Frame(None, MAIN_CODE, None, None, program.main, [],
                  {"k": "halt"}, kind="main",
                  versions_view={fid: program.code_version
                                 for fid in program.functions})
        ]
        self.state = "paused"        # paused | running | finished | error
        self.result: int | None = None

    # ------------------------------------------------------------------
    # 环境链 / 代码解析
    # ------------------------------------------------------------------

    def _chain(self) -> list[str]:
        """自栈底向栈顶的已挂入记录 OID；depth 0 = 最后一个。"""
        return [f.record for f in self.stack if f.record is not None]

    def _slot(self, depth: int, slot: int):
        chain = self._chain()
        if depth >= len(chain):
            raise LangRuntimeError(f"internal error: bad environment depth {depth}")
        rec_oid = chain[len(chain) - 1 - depth]
        slots = self.heap.records[rec_oid]
        if slot >= len(slots):
            raise LangRuntimeError(f"internal error: bad environment slot {slot}")
        return slots[slot]

    def code_root(self, code: tuple) -> ast.TExpr:
        if code[0] == "main":
            return self.program.main
        return self.table.get(code[1], code[2]).fn.body

    @staticmethod
    def code_at(root: ast.TExpr, path: list) -> ast.TExpr:
        node = root
        for key in path:
            node = node[key] if isinstance(key, int) else getattr(node, key)
        return node  # type: ignore[return-value]

    @property
    def at_safepoint(self) -> bool:
        if self.state == "finished":
            return True
        return self.state == "paused" and bool(self.stack) and self.stack[-1].call_entry

    def fid_trace(self) -> list[tuple[str, str]]:
        """调用栈底在前的 [(fid, version), ...]（只含激活帧）。"""
        return [(f.fid, f.version) for f in self.stack if f.kind == "act"]

    # ------------------------------------------------------------------
    # 推进
    # ------------------------------------------------------------------

    def run(
        self,
        step_budget: int | None = None,
        safepoint_hook: Callable[["Machine"], str | None] | None = None,
    ) -> int:
        """推进到结束 / 预算暂停 / 安全点暂停。返回本次归约的计步节点数。"""
        if self.state == "finished":
            raise MachineStateError("machine already finished")
        if self.state == "error":
            raise MachineStateError("machine is in error state")
        done = 0
        self.state = "running"
        try:
            while True:
                frame = self.stack[-1]
                if frame.count_steps and isinstance(frame.term, _COUNTED):
                    if self.steps >= self.step_limit:
                        self.state = "error"
                        err = StepLimitError(self.steps + 1, self.step_limit)
                        err.events = list(self.events)
                        err.fid_trace = self.fid_trace()  # type: ignore[attr-defined]
                        raise err
                    if step_budget is not None and done >= step_budget:
                        self.state = "paused"
                        return done
                    self.steps += 1
                    done += 1
                self._reduce(frame, frame.term, safepoint_hook)
                if self.state in ("finished", "paused"):
                    return done
        except LangRuntimeError as exc:
            self.state = "error"
            exc.fid_trace = self.fid_trace()  # type: ignore[attr-defined]
            exc.ir_node = type(self.stack[-1].term).__name__ if self.stack else None  # type: ignore[attr-defined]
            raise

    # -- 值交付：沿续延传播，可能连环等待/出帧；hook 在整个链条中保持 -------

    def _deliver(self, frame: Frame, v, hook) -> None:
        while True:
            tag = frame.k["k"]
            if tag == "halt":
                if isinstance(v, bool) or not isinstance(v, int):
                    raise LangRuntimeError(
                        f"program result must be an integer, got {type(v).__name__}"
                    )
                self.result = v
                self.state = "finished"
                return
            if tag == "frame_done":
                self.stack.pop()
                if not self.stack:
                    raise LangRuntimeError("internal: control stack underflow")
                frame = self.stack[-1]
                continue
            kd = frame.k
            if tag == "inits":
                node = self.code_at(self.code_root(frame.code), kd["np"])
                kd["acc"].append(v)
                if len(kd["acc"]) < len(node.inits):
                    i = len(kd["acc"])
                    frame.term = node.inits[i]
                    frame.path = kd["np"] + ["inits", i]
                else:
                    frame.record = self.heap.alloc_record(tuple(kd["acc"]), kd["schema"])
                    frame.term = node.body
                    frame.path = kd["np"] + ["body"]
                    frame.k = {"k": "frame_done"}
                return
            if tag == "begin":
                node = self.code_at(self.code_root(frame.code), kd["np"])
                i = kd["i"] + 1
                if i < len(node.exprs):
                    kd["i"] = i
                    frame.term = node.exprs[i]
                    frame.path = kd["np"] + ["exprs", i]
                    return
                frame.k = kd["inner"]
                continue
            if tag == "if_k":
                node = self.code_at(self.code_root(frame.code), kd["np"])
                c = self._as_int(v)
                suffix = "then_e" if c == 0 else "else_e"
                frame.term = getattr(node, suffix)
                frame.path = kd["np"] + [suffix]
                frame.k = kd["inner"]
                return
            if tag == "prim":
                node = self.code_at(self.code_root(frame.code), kd["np"])
                acc = kd["acc"] + self._as_int(v)
                i = kd["i"] + 1
                if i < len(node.args):
                    kd["i"] = i
                    kd["acc"] = acc
                    frame.term = node.args[i]
                    frame.path = kd["np"] + ["args", i]
                    return
                frame.k = kd["inner"]
                v = acc
                continue
            if tag == "call_func":
                node = self.code_at(self.code_root(frame.code), kd["np"])
                if node.args:
                    frame.k = {"k": "call_arg", "np": kd["np"], "i": 0,
                               "fn": v, "argv": [], "inner": kd["inner"]}
                    frame.term = node.args[0]
                    frame.path = kd["np"] + ["args", 0]
                    return
                # 无参调用：term 复位到调用点，使异常位置（如有）落在 TCall
                frame.term = node
                frame.path = kd["np"]
                self._push_activation(frame, v, [], kd["inner"])
                if hook is not None and hook(self) == "pause":
                    self.state = "paused"
                return
            if tag == "call_arg":
                node = self.code_at(self.code_root(frame.code), kd["np"])
                kd["argv"].append(v)
                nxt = kd["i"] + 1
                if nxt < len(node.args):
                    kd["i"] = nxt
                    frame.term = node.args[nxt]
                    frame.path = kd["np"] + ["args", nxt]
                    return
                # 实参齐备：term 复位到调用点，异常（非函数/元数）位置即 TCall
                frame.term = node
                frame.path = kd["np"]
                self._push_activation(frame, kd["fn"], kd["argv"], kd["inner"])
                if hook is not None and hook(self) == "pause":
                    self.state = "paused"
                return
            if tag == "cellset":
                iv = self._as_int(v)
                cell_oid = self._slot(kd["depth"], kd["slot"])
                self.heap.cell_set(cell_oid, iv)
                self.events.append(("set", kd["name"], iv))
                frame.k = kd["inner"]
                v = iv
                continue
            if tag == "fill":
                cell_oid = self._slot(kd["depth"], kd["slot"])
                self.heap.fill_cell(cell_oid, v, kd["name"])
                frame.k = kd["inner"]
                v = 0
                continue
            if tag == "mkcell":
                frame.k = kd["inner"]
                v = self.heap.alloc_cell(v, filled=True)
                continue
            if tag == "clo_env":
                kd["acc"].append(v)
                node = self.code_at(self.code_root(frame.code), kd["np"])
                i = kd["i"] + 1
                if i < len(node.env):
                    kd["i"] = i
                    frame.term = node.env[i]
                    frame.path = kd["np"] + ["env", i]
                    return
                frame.count_steps = kd["restore_count"]
                frame.k = kd["inner"]
                v = self._make_closure_value(frame, kd["fid"], tuple(kd["acc"]))
                continue
            raise LangRuntimeError(f"internal: bad continuation {tag!r}")

    # -- 单节点归约 --------------------------------------------------------

    def _reduce(self, frame: Frame, e: ast.TExpr, hook) -> None:
        frame.call_entry = False

        if isinstance(e, ast.TConst):
            self._deliver(frame, e.value, hook)
            return
        if isinstance(e, ast.TGet):
            self._deliver(frame, self._slot(e.depth, e.slot), hook)
            return
        if isinstance(e, ast.TCellGet):
            raw = self._slot(e.depth, e.slot)
            self._deliver(frame, self.heap.cell_get(raw, e.name), hook)
            return
        if isinstance(e, ast.TCellSet):
            frame.k = {"k": "cellset", "depth": e.depth, "slot": e.slot,
                       "name": e.name, "inner": frame.k}
            frame.term = e.value
            frame.path = frame.path + ["value"]
            return
        if isinstance(e, ast.TPrim):
            if e.op != "add":
                raise LangRuntimeError(f"unknown primitive {e.op!r}")
            if not e.args:
                self._deliver(frame, 0, hook)
                return
            frame.k = {"k": "prim", "np": list(frame.path), "i": 0,
                       "acc": 0, "inner": frame.k}
            frame.term = e.args[0]
            frame.path = frame.k["np"] + ["args", 0]
            return
        if isinstance(e, ast.TMkCell):
            if e.value is None:
                self._deliver(frame, self.heap.alloc_cell(filled=False), hook)
                return
            frame.k = {"k": "mkcell", "inner": frame.k}
            frame.term = e.value
            frame.path = frame.path + ["value"]
            return
        if isinstance(e, ast.TFillCell):
            frame.k = {"k": "fill", "depth": e.depth, "slot": e.slot,
                       "name": e.name, "inner": frame.k}
            frame.term = e.value
            frame.path = frame.path + ["value"]
            return
        if isinstance(e, ast.TMkClosure):
            if e.env:
                frame.k = {"k": "clo_env", "fid": e.fid, "np": list(frame.path),
                           "i": 0, "acc": [], "restore_count": frame.count_steps,
                           "inner": frame.k}
                frame.count_steps = False  # 环境槽抽取是布局动作，不计步
                frame.term = e.env[0]
                frame.path = frame.k["np"] + ["env", 0]
            else:
                oid = self._make_closure_value(frame, e.fid, ())
                self._deliver(frame, oid, hook)
            return
        if isinstance(e, ast.TCall):
            frame.k = {"k": "call_func", "np": list(frame.path), "inner": frame.k}
            frame.term = e.func
            frame.path = frame.k["np"] + ["func"]
            return
        if isinstance(e, ast.TIf0):
            frame.k = {"k": "if_k", "np": list(frame.path), "inner": frame.k}
            frame.term = e.cond
            frame.path = frame.path + ["cond"]
            return
        if isinstance(e, ast.TWithFrame):
            w = Frame(
                record=None,
                code=frame.code,
                fid=frame.fid,
                version=frame.version,
                term=e.inits[0] if e.inits else e.body,
                path=(list(frame.path) + ["inits", 0]) if e.inits
                else list(frame.path) + ["body"],
                k=None,  # type: ignore[arg-type]
                kind="frame",
                versions_view=frame.versions_view,  # 继承发起帧的冻结视图
            )
            if e.inits:
                w.k = {"k": "inits", "np": list(frame.path), "acc": [],
                       "schema": e.schema_id}
            else:
                w.record = self.heap.alloc_record((), e.schema_id)
                w.k = {"k": "frame_done"}
            self.stack.append(w)
            return
        if isinstance(e, ast.TBegin):
            if not e.exprs:
                self._deliver(frame, 0, hook)
                return
            frame.k = {"k": "begin", "np": list(frame.path), "i": 0,
                       "inner": frame.k}
            frame.term = e.exprs[0]
            frame.path = frame.k["np"] + ["exprs", 0]
            return
        raise LangRuntimeError(f"cannot execute IR node {type(e).__name__}")

    # -- 闭包 / 激活 -------------------------------------------------------

    def _make_closure_value(self, frame: Frame, fid: str, record_slots: tuple) -> str:
        view = frame.versions_view
        if view is None or fid not in view:
            raise LangRuntimeError(f"unknown function id {fid!r}")
        version = view[fid]
        fn = self.table.get(fid, version).fn
        if len(record_slots) != fn.n_capture:
            raise LangRuntimeError(
                f"closure record for {fid} has {len(record_slots)} slots, "
                f"expected {fn.n_capture}"
            )
        rec_oid = self.heap.alloc_record(tuple(record_slots), f"capture:{fid}:{version}")
        return self.heap.alloc_closure(fid, rec_oid, version)

    def _push_activation(self, caller: Frame, clo_oid: object, argv: list,
                         inner: dict) -> None:
        if not isinstance(clo_oid, str) or clo_oid not in self.heap.closures:
            raise LangRuntimeError("attempt to call non-function value")
        fid, cap_rec_oid, version = self.heap.closure_info(clo_oid)
        fn = self.table.get(fid, version).fn
        if len(argv) != len(fn.params):
            raise LangRuntimeError(
                f"function {fn.fid} expects {len(fn.params)} args, got {len(argv)}"
            )
        slots = list(self.heap.records[cap_rec_oid])
        for i, a in enumerate(argv):
            if fn.mutable_params[i]:
                slots.append(self.heap.alloc_cell(a, filled=True))
            else:
                slots.append(a)
        if len(slots) != fn.n_slots:
            raise LangRuntimeError(
                f"activation record for {fn.fid} has {len(slots)} slots, "
                f"expected {fn.n_slots}"
            )
        rec_oid = self.heap.alloc_record(tuple(slots), f"act:{fid}:{version}")
        caller.k = inner  # 调用方帧等待函数返回值
        # 新激活帧冻结*调用时刻*的版本视图：此后在该帧内构造闭包，
        # 一律按此视图绑定版本，不受后续升级影响。
        view = dict(self.table.current)
        self.stack.append(Frame(
            record=rec_oid,
            code=("fn", fid, version),
            fid=fid,
            version=version,
            term=fn.body,
            path=[],
            k={"k": "frame_done"},
            kind="act",
            call_entry=True,
            versions_view=view,
        ))

    @staticmethod
    def _as_int(v) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise LangRuntimeError(f"integer expected, got {type(v).__name__}")
        return v


def run_machine(
    program: ast.Program,
    step_limit: int = 10_000,
) -> tuple[int, list[tuple[str, str, int]], Machine]:
    """一次性跑到结束的便捷入口。"""
    m = Machine(program, step_limit=step_limit)
    m.run()
    assert m.result is not None
    return m.result, m.events, m

"""显式栈机器：在堆 + 续体栈上执行闭包转换后的 IR。

与旧式递归下降解释器不同，本机器：

* **不使用 Python 调用栈保存运行态**——一条 ``while`` 循环 + 显式续体帧
  （:class:`KFrame` 的各子类），因此 500+ 层显式调用帧与运行中暂停都不依赖
  ``sys.setrecursionlimit``；
* 所有共享状态（cell / 闭包 / 记录）在 :class:`~recenv.heap.Heap` 上，
  栈帧只持记录引用；
* **安全点 = 调用入口**（:meth:`Machine._enter_call`）。只有在安全点才允许：
  暂停、代码升级。进入被调函数前压入 :class:`KReturn` 标记并钉住本次调用的
  ``fkey``（代码版本）；旧栈帧因此始终跑旧代码，新调用经迁移后的新闭包进入
  新版本，环境引用互不混淆；
* 步数口径与 :mod:`recenv.target_interp` 完全一致（每个源语言归约一步）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ast_nodes as ast
from .codebundle import MAIN_FKEY, CodeBundle, build_bundle
from .errors import (
    FrameLimitError,
    LangRuntimeError,
    StepLimitError,
)
from .heap import CellObj, Heap, Ref

DEFAULT_FRAME_LIMIT = 100_000

# 计步的 IR 节点类型（与 target_interp 一一对应）
_COUNTED_TYPES = frozenset({
    ast.TConst, ast.TGet, ast.TCellGet, ast.TCellSet, ast.TPrim,
    ast.TMkClosure, ast.TCall, ast.TIf0, ast.TWithFrame,
})


# ---------------------------------------------------------------------------
# 续体帧
# ---------------------------------------------------------------------------


@dataclass
class KFrame:
    fkey: str          # 该续体所属的执行函数（MAIN_FKEY 表示主表达式）
    env: Ref | None


@dataclass
class KPrim(KFrame):
    e: ast.TPrim
    idx: int = 0
    vals: list = field(default_factory=list)


@dataclass
class KCellSet(KFrame):
    e: ast.TCellSet


@dataclass
class KMkCell(KFrame):
    e: ast.TMkCell


@dataclass
class KFill(KFrame):
    e: ast.TFillCell


@dataclass
class KClosure(KFrame):
    e: ast.TMkClosure
    idx: int = 0
    slots: list = field(default_factory=list)


@dataclass
class KCall(KFrame):
    e: ast.TCall
    phase: int = 0     # 0=待求函数；>=1=待求第 phase-1 个实参
    vals: list = field(default_factory=list)


@dataclass
class KIf(KFrame):
    e: ast.TIf0


@dataclass
class KWithFrame(KFrame):
    e: ast.TWithFrame
    idx: int = 0
    slots: list = field(default_factory=list)


@dataclass
class KBegin(KFrame):
    e: ast.TBegin
    idx: int = 0


@dataclass
class KEnterCall:
    """调用入口安全点的事务态：函数与实参已全部求值，但尚未进入被调方。

    机器控制位为 :data:`CALL_ENTRY`。下一拍在此先应用待挂升级（可失败并完整
    回滚——此时调用尚未提交，失败后重拍仍是同一安全点），再进入被调函数。
    """

    fkey: str
    env: Ref | None
    callee: object
    args: list


class _CallEntrySentinel:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<call-entry safepoint>"


CALL_ENTRY = _CallEntrySentinel()


@dataclass
class KReturn:
    """调用边界标记。安全点位于压入本帧的 :meth:`Machine._enter_call` 中。

    钉住被调用闭包的 fkey / 版本 / 闭包 OID 与调用者深度：旧栈帧恢复后继续
    引用其代码版本，升级不影响在途帧。

    ``bottom`` 标记一次由机器外部驱动的调用。在暂停状态下发起的*旁路*新调用
    （迁移后的新闭包在旧栈帧仍在栈上时运行）会保存父执行的控制现场，待旁路
    调用结束后原样恢复，因此同一执行例中可以同时观察到旧栈帧与新闭包。
    """

    fkey: str
    unit: str | None
    version: int | None
    closure_oid: int
    caller_depth: int
    bottom: bool = False
    saved_control: ast.TExpr | None = None
    saved_env: Ref | None = None
    saved_cur: str | None = None
    saved_depth: int | None = None


@dataclass
class _PendingUpgrade:
    unit: str
    rules: object
    new_program: ast.Program
    failpoint: str | None
    failhook: object


class Machine:
    """可暂停、可序列化、可在安全点热升级的显式栈机器。"""

    def __init__(
        self,
        program: ast.Program | None = None,
        bundle: CodeBundle | None = None,
        *,
        step_limit: int = 10_000,
        frame_limit: int = DEFAULT_FRAME_LIMIT,
        heap_limit: int = 100_000,
        heap: Heap | None = None,
    ) -> None:
        if bundle is None:
            if program is None:
                raise ValueError("Machine requires program or bundle")
            bundle = build_bundle(program)
        self.bundle = bundle
        self.heap = heap if heap is not None else Heap(limit=heap_limit)
        self.step_limit = step_limit
        self.frame_limit = frame_limit
        self.steps = 0
        self.events: list[tuple[str, str, int]] = []
        self.frames: list = []
        self.control: ast.TExpr | None = None
        self.env: Ref | None = None
        self.cur_fkey: str = MAIN_FKEY
        self.depth = 0
        self.status = "ready"      # ready | running | paused | done | error
        self.result: int | None = None
        self._pending: _PendingUpgrade | None = None
        self._seg_budget: int | None = None
        self._seg_used = 0
        self._pause_wanted = False
        # 最近一次升级迁移出的句柄（实例 -> 新闭包引用，按成员名给出）
        self.last_handles: list[dict[str, Ref]] = []
        self.applied_upgrades: list[tuple[str, int, int]] = []
        # 旁路调用（在暂停的旧栈之上运行新闭包）簿记
        self.side_result: int | None = None
        self._side_event_start = 0

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        if self.status != "ready":
            raise RuntimeError(f"machine already started (status={self.status})")
        self.frames.append(KReturn(MAIN_FKEY, None, None, -1, 0, bottom=True))
        self.control = self.bundle.main
        self.env = None
        self.cur_fkey = MAIN_FKEY
        self.status = "running"

    def invoke(self, closure_ref: Ref, args: list, *, side: bool = False) -> "Machine":
        """在安全点对现存（通常是迁移后的新）闭包发起一次机器外部驱动调用。

        * 程序已结束（``done``）：作为新的顶层调用继续机器；
        * 机器暂停中（``paused``）且 ``side=True``：在保留旧栈帧与控制现场的
          前提下，于旧栈之上旁路运行新闭包；旁路调用结束后机器自动回到
          ``paused``，旧现场原样恢复，随后可继续恢复主执行。
        """
        if self.status not in ("done", "paused"):
            raise RuntimeError(
                f"invoke is only allowed at a safepoint, status={self.status}"
            )
        if self.status == "done":
            # 程序已结束是一个安全点；此处可直接应用待挂升级。
            if self._pending is not None:
                self._apply_pending(None)
            self.result = None
            self._enter_call(closure_ref, list(args), bottom=True)
            self.status = "running"
            return self
        # paused：旁路调用。升级必须先在调用入口安全点显式应用（或无待挂升级）。
        if self._pending is not None:
            raise RuntimeError(
                "an upgrade is pending; call apply_pending_upgrade() at the "
                "call-entry safepoint before invoke()"
            )
        if not side:
            raise RuntimeError(
                "machine is paused mid-execution; pass side=True to run a closure "
                "alongside the suspended stack, or resume with run_segment()"
            )
        self._side_event_start = len(self.events)
        self._enter_call(
            closure_ref, list(args), bottom=True,
            side_ctx=(self.control, self.env, self.cur_fkey, self.depth),
        )
        self.status = "running"
        return self

    def side_result_value(self) -> int | None:
        """最近一次旁路调用的返回值（未发生过旁路调用为 None）。"""
        return self.side_result

    def run_side(self, step_limit: int | None = None) -> tuple[int, list]:
        """在暂停的旧栈之上把当前旁路调用跑完，机器回到 ``paused``。

        返回旁路调用自己的 (返回值, 它新增的事件)。旧栈与控制现场保持不变。
        """
        if self.status != "running":
            raise RuntimeError("run_side requires a running side invocation")
        start_events = len(self.events)
        # 旁路调用一口气跑完：不在内部安全点暂停（步数预算仍然生效）。
        self._seg_budget = None
        self._seg_used = 0
        self._pause_wanted = False
        self._drive()
        if self.status != "paused":
            raise RuntimeError(
                f"side invocation did not return to a paused state: {self.status}"
            )
        result = self.side_result
        side_events = self.events[start_events:]
        if isinstance(result, bool) or not isinstance(result, int):
            self.status = "error"
            raise LangRuntimeError(
                f"side result must be an integer, got {type(result).__name__}"
            )
        return result, list(side_events)

    # -- 升级请求 ----------------------------------------------------------

    def request_upgrade(self, unit, new_program, rules, *,
                        failpoint: str | None = None, failhook=None) -> None:
        """登记升级请求；在下一个安全点（调用入口）原子应用。"""
        if self._pending is not None:
            raise RuntimeError("an upgrade is already pending")
        self._pending = _PendingUpgrade(
            unit=unit, rules=rules, new_program=new_program,
            failpoint=failpoint, failhook=failhook,
        )

    def apply_pending_upgrade(self) -> list:
        """在当前暂停的安全点立即应用已登记的升级（不推进执行）。

        返回每实例的新句柄（成员名 -> 闭包引用）。应用后旧栈帧保持不变，
        可用 :meth:`invoke` 在其之上旁路运行新闭包，或 :meth:`run_to_completion`
        继续旧执行。
        """
        if self.status != "paused":
            raise RuntimeError("apply_pending_upgrade is only valid while paused")
        if self._pending is None:
            raise RuntimeError("no pending upgrade")
        if self.control is not CALL_ENTRY:
            raise RuntimeError(
                "upgrades only apply at a call-entry safepoint; current paused "
                "state is not a call entry"
            )
        k = self.frames[-1]
        if not isinstance(k, KEnterCall):
            raise RuntimeError("paused safepoint is missing its call holder")
        self._apply_pending(k.callee)
        return self.last_handles

    def _apply_pending(self, callee: Ref | None) -> None:
        from .migrate import apply_upgrade

        req = self._pending
        self._pending = None
        handles = apply_upgrade(
            self, req.unit, req.new_program, req.rules,
            failpoint=req.failpoint, failhook=req.failhook,
        )
        self.last_handles = handles

    # -- 执行驱动 ----------------------------------------------------------

    def tick(self) -> None:
        self.steps += 1
        if self._seg_budget is not None:
            self._seg_used += 1
            if self._seg_used >= self._seg_budget:
                self._pause_wanted = True
        if self.steps > self.step_limit:
            self.status = "error"
            raise StepLimitError(
                self.steps, self.step_limit, events=list(self.events)
            )

    def call_trace(self) -> tuple[tuple[str, str | None, int | None], ...]:
        """当前在途调用栈：自底向上 (fkey, unit, version)。"""
        return tuple(
            (f.fkey, f.unit, f.version)
            for f in self.frames
            if isinstance(f, KReturn) and not f.bottom
        )

    def run_to_completion(self) -> tuple[int, list[tuple[str, str, int]]]:
        """连续执行到程序结束；语义与 eval_target 相同（含异常类型/步数口径）。"""
        if self.status == "ready":
            self.start()
        self._seg_budget = None
        self._seg_used = 0
        self._pause_wanted = False
        self._drive()
        if isinstance(self.result, bool) or not isinstance(self.result, int):
            self.status = "error"
            raise LangRuntimeError(
                f"program result must be an integer, got {type(self.result).__name__}"
            )
        return self.result, list(self.events)

    def run_segment(self, budget: int) -> str:
        """执行至多 ``budget`` 个计步，在预算用满后的第一个安全点暂停。

        返回 ``"done"`` 或 ``"paused"``。暂停点只能是调用入口，因此任意切分
        预算反复暂停/恢复，最终值、事件序列与异常位置都与连续执行一致。
        """
        if not isinstance(budget, int) or budget <= 0:
            raise ValueError("segment budget must be a positive integer")
        if self.status == "ready":
            self.start()
        if self.status == "done":
            return "done"
        self._seg_budget = budget
        self._seg_used = 0
        self._pause_wanted = False
        self._drive()
        return "done" if self.status == "done" else "paused"

    def _drive(self) -> None:
        self.status = "running"
        while self.control is not None:
            self._step_once()
            if self.status != "running":
                return

    # -- 单步微转移 --------------------------------------------------------

    def _step_once(self) -> None:
        e = self.control
        if e is CALL_ENTRY:
            # 安全点：预算用满则先在此暂停（不进入被调方、不消费调用）。
            if self._pause_wanted:
                self.status = "paused"
                return
            self._do_call_entry()
            return
        if type(e) in _COUNTED_TYPES:
            self.tick()

        if isinstance(e, ast.TConst):
            self._finish(e.value)
        elif isinstance(e, ast.TGet):
            self._finish(self._slot_raw(self.env, e.depth, e.slot))
        elif isinstance(e, ast.TCellGet):
            ref = self._slot_raw(self.env, e.depth, e.slot)
            cell = self._as_cell(ref)
            if not cell.filled:
                raise self._rt(
                    f"read of recursive slot {e.name!r} before initialization"
                )
            self._finish(cell.value)
        elif isinstance(e, ast.TCellSet):
            self.frames.append(KCellSet(self.cur_fkey, self.env, e))
            self.control = e.value
        elif isinstance(e, ast.TPrim):
            if e.op != "add":
                raise self._rt(f"unknown primitive {e.op!r}")
            if not e.args:
                self._finish(0)
            else:
                k = KPrim(self.cur_fkey, self.env, e)
                self.frames.append(k)
                self.control = e.args[0]
        elif isinstance(e, ast.TMkCell):
            if e.value is None:
                self._finish(self.heap.new_cell(None, filled=False, kind="rec"))
            else:
                self.frames.append(KMkCell(self.cur_fkey, self.env, e))
                self.control = e.value
        elif isinstance(e, ast.TFillCell):
            self.frames.append(KFill(self.cur_fkey, self.env, e))
            self.control = e.value
        elif isinstance(e, ast.TMkClosure):
            if not e.env:
                self._make_closure(e, self.env, [])
            else:
                self.frames.append(KClosure(self.cur_fkey, self.env, e))
                self.control = e.env[0]
        elif isinstance(e, ast.TCall):
            self.frames.append(KCall(self.cur_fkey, self.env, e))
            self.control = e.func
        elif isinstance(e, ast.TIf0):
            self.frames.append(KIf(self.cur_fkey, self.env, e))
            self.control = e.cond
        elif isinstance(e, ast.TWithFrame):
            if not e.inits:
                rec = self.heap.new_record([], parent=self.env, role="frame")
                self.env = rec
                self.control = e.body
            else:
                self.frames.append(KWithFrame(self.cur_fkey, self.env, e))
                self.control = e.inits[0]
        elif isinstance(e, ast.TBegin):
            if not e.exprs:
                self._finish(0)
            else:
                self.frames.append(KBegin(self.cur_fkey, self.env, e))
                self.control = e.exprs[0]
        else:  # pragma: no cover - 防御
            raise self._rt(f"cannot execute IR node {type(e).__name__}")

    # -- 值回到续体栈（完全迭代，深返回链不占用 Python 调用栈） ------------

    def _finish(self, value) -> None:
        """把一个值沿显式续体栈向栈底传播。

        纯 ``while`` 循环：被调函数返回、嵌套 TPrim/TBegin 的“值继续上送”都在
        本循环内迭代完成，不递归调用自身，因此 500+ 层帧返回不需要 Python 栈。
        """
        while True:
            if not self.frames:
                self.result = value
                self.control = None
                self.status = "done"
                return
            k = self.frames[-1]
            if isinstance(k, KReturn):
                if k.bottom and k.saved_control is not None:
                    # 旁路调用结束：恢复被挂起的父执行现场，机器回到 paused
                    self.frames.pop()
                    self.side_result = value
                    self.control = k.saved_control
                    self.env = k.saved_env
                    self.cur_fkey = k.saved_cur or MAIN_FKEY
                    self.depth = k.saved_depth or 0
                    self.status = "paused"
                    return
                if k.bottom:
                    self.frames.pop()
                    self.result = value
                    self.control = None
                    self.env = None
                    self.cur_fkey = MAIN_FKEY
                    self.depth = 0
                    self.status = "done"
                    return
                # 被调函数返回：弹出标记，恢复调用者深度，值继续向栈底传播
                self.frames.pop()
                self.depth = k.caller_depth
                continue

            self.frames.pop()
            self.cur_fkey = k.fkey
            self.env = k.env
            action = self._dispatch_return(k, value)
            if action is None:
                # 已设置新的控制表达式（继续求子式）或进入安全点：停止传播
                return
            # 否则 action 为 ("propagate", 新值)：沿栈继续上送
            value = action[1]

    def _dispatch_return(self, k, value):
        """返回 None（已设置新控制位）或 ("propagate", value)。"""
        if isinstance(k, KPrim):
            k.vals.append(self._as_int(value))
            k.idx += 1
            if k.idx < len(k.e.args):
                self.frames.append(k)
                self.control = k.e.args[k.idx]
                return None
            return ("propagate", sum(k.vals))
        if isinstance(k, KCellSet):
            iv = self._as_int(value)
            cell = self._as_cell(self._slot_raw(k.env, k.e.depth, k.e.slot))
            cell.value = iv
            cell.filled = True
            self.events.append(("set", k.e.name, iv))
            return ("propagate", iv)
        if isinstance(k, KMkCell):
            return ("propagate", self.heap.new_cell(value, filled=True, kind="cell"))
        if isinstance(k, KFill):
            cell = self._as_cell(self._slot_raw(k.env, k.e.depth, k.e.slot))
            if cell.filled:
                raise self._rt(f"recursive slot {k.e.name!r} already initialized")
            if value is not None and not isinstance(value, (int, Ref)):
                raise self._rt("bad value for recursive slot")
            cell.value = value
            cell.filled = True
            return ("propagate", 0)
        if isinstance(k, KClosure):
            k.slots.append(value)
            k.idx += 1
            if k.idx < len(k.e.env):
                self.frames.append(k)
                self.control = k.e.env[k.idx]
                return None
            self._make_closure(k.e, k.env, k.slots)  # _make_closure 调 _finish
            return None
        if isinstance(k, KCall):
            k.vals.append(value)
            k.phase += 1
            if k.phase <= len(k.e.args):
                self.frames.append(k)
                self.control = k.e.args[k.phase - 1]
                return None
            # 函数与实参全部就绪：停到调用入口安全点（不进入被调方）
            self.frames.append(KEnterCall(
                fkey=k.fkey, env=k.env, callee=k.vals[0], args=k.vals[1:]
            ))
            self.control = CALL_ENTRY
            return None
        if isinstance(k, KIf):
            c = self._as_int(value)
            self.control = k.e.then_e if c == 0 else k.e.else_e
            return None
        if isinstance(k, KWithFrame):
            k.slots.append(value)
            k.idx += 1
            if k.idx < len(k.e.inits):
                self.frames.append(k)
                self.control = k.e.inits[k.idx]
                return None
            rec = self.heap.new_record(list(k.slots), parent=k.env, role="frame")
            self.env = rec
            self.control = k.e.body
            return None
        if isinstance(k, KBegin):
            k.idx += 1
            if k.idx < len(k.e.exprs):
                self.frames.append(k)
                self.control = k.e.exprs[k.idx]
                return None
            return ("propagate", value)
        raise self._rt(f"internal: bad continuation {type(k).__name__}")

    # -- 调用入口（唯一安全点） --------------------------------------------

    def _do_call_entry(self) -> None:
        """在调用入口安全点：先应用待挂升级（可整体失败/回滚），再进入被调方。

        升级失败时 KEnterCall 仍在栈顶、控制位仍是 CALL_ENTRY，机器停留在同一
        安全点，可稍后重试或继续旧版本——本次调用尚未发生。
        """
        k = self.frames[-1]
        assert isinstance(k, KEnterCall)
        if self._pending is not None:
            try:
                self._apply_pending(k.callee)
            except BaseException:
                # 升级在提交前完整回滚；调用尚未提交，机器仍停在本安全点。
                self.status = "paused"
                raise
        # 进入被调方：消费安全点态，callee 是升级前已求出的闭包（旧版本旧代码；
        # 升级后新发起的调用会从已改线的槽中取到新闭包）。
        self.frames.pop()
        self._enter_call(k.callee, k.args, bottom=False)

    def _enter_call(self, callee, args, *, bottom: bool,
                    side_ctx=None) -> None:
        if not isinstance(callee, Ref) or not self.heap.is_closure(callee):
            raise self._rt("attempt to call non-function value")
        clo = self.heap.closure(callee)
        fn = self.bundle.get(clo.fkey)
        if len(args) != len(fn.params):
            raise self._rt(
                f"function {fn.fkey} expects {len(fn.params)} args, "
                f"got {len(args)}"
            )
        checked: list = []
        for i, a in enumerate(args):
            if isinstance(a, bool) or not isinstance(a, (int, Ref)):
                raise self._rt(f"bad argument value: {type(a).__name__}")
            if fn.mutable_params[i]:
                checked.append(self.heap.new_cell(a, filled=True, kind="cell"))
            else:
                checked.append(a)
        cap = list(self.heap.record(clo.record).slots)
        n_total = len(cap) + len(checked)
        if n_total != fn.n_slots:
            raise self._rt(
                f"activation record for {fn.fkey} has {n_total} slots, "
                f"expected {fn.n_slots}"
            )
        # ---- 安全点（应用待挂升级 / 暂停）已统一在 CALL_ENTRY 处理；
        #      进入此处时调用已经提交，_enter_call 只负责建立激活帧。 ------
        caller_depth = self.depth
        if not bottom:
            self.depth += 1
            if self.depth > self.frame_limit:
                self.status = "error"
                raise FrameLimitError(self.depth, self.frame_limit)
        act = self.heap.new_record(cap + checked, parent=None, role="activation")
        self.heap.record(act).tag = fn.fkey
        if side_ctx is not None:
            s_control, s_env, s_cur, s_depth = side_ctx
        else:
            s_control = s_env = s_cur = s_depth = None
        marker = KReturn(
            fkey=fn.fkey, unit=fn.unit, version=fn.version,
            closure_oid=callee.oid,
            caller_depth=caller_depth,
            bottom=bottom,
            saved_control=s_control, saved_env=s_env,
            saved_cur=s_cur, saved_depth=s_depth,
        )
        self.frames.append(marker)
        self.env = act
        self.control = fn.body
        self.cur_fkey = fn.fkey

    # -- 闭包构造 ----------------------------------------------------------

    def _make_closure(self, e: ast.TMkClosure, env: Ref | None, slots: list) -> None:
        fn = self.bundle.get(e.fid)
        if len(slots) != fn.n_capture:
            raise self._rt(
                f"closure record for {e.fid} has {len(slots)} slots, "
                f"expected {fn.n_capture}"
            )
        rec = self.heap.new_record(list(slots), parent=None, role="capture")
        ref = self.heap.new_closure(
            e.fid, rec, unit=fn.unit, version=fn.version,
            member=(fn.member if fn.unit is not None else None),
        )
        self._finish(ref)

    # -- 环境访问 / 类型辅助 -----------------------------------------------

    def _slot_raw(self, env: Ref | None, depth: int, slot: int):
        r = env
        d = 0
        while r is not None:
            if d == depth:
                rec = self.heap.record(r)
                if slot >= len(rec.slots):
                    raise self._rt(f"internal error: bad environment slot {slot}")
                return rec.slots[slot]
            r = self.heap.record(r).parent
            d += 1
        raise self._rt(f"internal error: bad environment depth {depth}")

    def _as_cell(self, ref) -> CellObj:
        if not isinstance(ref, Ref):
            raise self._rt(f"expected cell, got inline {type(ref).__name__}")
        obj = self.heap.get(ref)
        if not isinstance(obj, CellObj):
            raise self._rt(f"expected cell at heap object #{ref.oid}")
        return obj

    def _as_int(self, v) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise self._rt(f"integer expected, got {type(v).__name__}")
        return v

    def _rt(self, msg: str) -> LangRuntimeError:
        self.status = "error"
        err = LangRuntimeError(msg)
        raise err

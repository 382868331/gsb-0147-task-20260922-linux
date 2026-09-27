"""目标 IR 解释器：在显式环境布局（帧链 + 槽位 + Cell）上执行转换输出。

值只有整数、:class:`Closure`、:class:`Cell`。副作用事件与源解释器使用
完全相同的 ``("set", 名字, 整数)`` 记录格式，便于逐事件对照。

本解释器只依赖 :mod:`recenv.ast_nodes` 中的 IR 定义，不调用转换器。

尾调用消除
----------
转换器用 ``TCall.tail`` 标出尾位置调用。尾调用的被调用者参数一旦求值
完毕，当前激活帧之后不会再被使用（它的活数据——被新闭包捕获的槽——
已经按值或按 Cell 身份复制进被调用者的捕获记录），因此本解释器用
trampoline 复用激活帧：连续尾调用始终只占一层调用现场，而不是简单地
调高递归深度上限。普通调用保持原有嵌套求值行为与 Python 调用栈不变。

运行占用通过解释器上的统计量如实记录（见 :class:`FrameStats`）：
``depth`` / ``peak_depth`` 是当前/峰值存活函数激活帧数，
``tail_reuses`` 是复用调用现场的尾调用次数。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import LangRuntimeError, StepLimitError

# 与源解释器相同：提升栈余量，保证 10000 步预算先于递归深度报错触发。
sys.setrecursionlimit(max(100_000, sys.getrecursionlimit()))

DEFAULT_STEP_LIMIT = 10_000


class Cell:
    __slots__ = ("value", "filled")

    def __init__(self, value=None, filled: bool = True) -> None:
        self.value = value
        self.filled = filled


@dataclass
class Closure:
    fid: str
    record: tuple  # 捕获记录（tuple，槽内为 int / Closure / Cell）


@dataclass
class FrameStats:
    """一次运行中真实保留的调用层统计（每次 run 新建，互不影响）。

    Attributes:
        depth: 当前存活的函数激活帧数（主表达式不计入）
        peak_depth: 运行期间 depth 的峰值，即实际同时占用的最大调用层数
        tail_reuses: 尾调用复用前一调用现场的次数
        calls: 总调用次数（普通调用 + 尾调用）
    """

    depth: int = 0
    peak_depth: int = 0
    tail_reuses: int = 0
    calls: int = 0


class _TailCall:
    """尾位置信号：当前激活帧已可释放，转去激活 callee。"""

    __slots__ = ("callee", "args")

    def __init__(self, callee: object, args: list[object]) -> None:
        self.callee = callee
        self.args = args


def run_program(
    program: ast.Program, step_limit: int = DEFAULT_STEP_LIMIT
) -> tuple[int, list[tuple[str, str, int]]]:
    """执行 :func:`recenv.convert.convert` 的输出（带尾调用消除）。

    Returns:
        (整数结果, 副作用事件序列)
    """
    value, events, _stats = run_program_tracked(program, step_limit)
    return value, events


def run_program_tracked(
    program: ast.Program, step_limit: int = DEFAULT_STEP_LIMIT
) -> tuple[int, list[tuple[str, str, int]], FrameStats]:
    """与 :func:`run_program` 相同，但额外返回调用层占用统计。"""
    interp = _TargetInterp(program, step_limit)
    try:
        result = interp._drive(program.main, (), tail=True, owns_frame=False)
    except StepLimitError as e:
        e.events = list(interp.events)
        raise
    if isinstance(result, bool) or not isinstance(result, int):
        raise LangRuntimeError(
            f"program result must be an integer, got {type(result).__name__}"
        )
    return result, interp.events, interp.stats


class _TargetInterp:
    def __init__(self, program: ast.Program, step_limit: int) -> None:
        self.program = program
        self.steps = 0
        self.limit = step_limit
        self.events: list[tuple[str, str, int]] = []
        self.stats = FrameStats()

    def tick(self) -> None:
        self.steps += 1
        if self.steps > self.limit:
            raise StepLimitError(self.steps, self.limit)

    # chain: tuple of tuple（帧 0 为链顶）
    def slot(self, chain: tuple[tuple, ...], depth: int, slot: int):
        if depth >= len(chain):
            raise LangRuntimeError(f"internal error: bad environment depth {depth}")
        record = chain[depth]
        if slot >= len(record):
            raise LangRuntimeError(f"internal error: bad environment slot {slot}")
        return record[slot]

    def eval(
        self,
        e: ast.TExpr,
        chain: tuple[tuple, ...],
        count: bool = True,
        tail: bool = False,
        owns_frame: bool = False,
    ) -> object:
        """统一求值入口（也保留为测试/子类的包装点）。

        tail=True 时启用尾调用消除；普通子表达式一律以 tail=False 求成
        具体值。返回值要么是一个具体语言值，要么（内部使用）是 _TailCall。
        """
        return self._drive(e, chain, tail=tail, count=count, owns_frame=owns_frame)

    def _drive(
        self,
        e: ast.TExpr,
        chain: tuple[tuple, ...],
        tail: bool,
        count: bool = True,
        owns_frame: bool = False,
    ) -> object:
        """尾位置 trampoline。

        tail=True 时，尾调用（以及包裹尾调用的 if0 / let 帧 / begin 组体）
        最终产生 _TailCall；本循环接住它，*不增加存活激活帧数*，直接用
        被调用者的函数体与新激活帧替换当前执行现场。普通返回值原样返回。
        """
        entered_frame = False
        try:
            while True:
                result = self._eval_raw(e, chain, tail=tail, count=count)
                if not tail or not isinstance(result, _TailCall):
                    return result
                # 尾调用：复用当前调用现场，层数不增加
                self.stats.calls += 1
                self.stats.tail_reuses += 1
                if not owns_frame:
                    # 从主层（非函数帧）第一次跳入函数：仍要建立第 1 层，
                    # 此后的连续尾调用都复用这一层
                    self.stats.depth += 1
                    if self.stats.depth > self.stats.peak_depth:
                        self.stats.peak_depth = self.stats.depth
                    owns_frame = True
                    entered_frame = True
                e, chain = self._enter(result.callee, result.args)
                count = True
                tail = True
        finally:
            if entered_frame:
                self.stats.depth -= 1

    def _eval_raw(
        self,
        e: ast.TExpr,
        chain: tuple[tuple, ...],
        tail: bool,
        count: bool = True,
    ) -> object:
        # 计步口径与源解释器一一对应：每个源语言归约步计 1。
        #   Let/LetRec 节点 -> TWithFrame（计 1）
        #   Lambda 求值    -> TMkClosure（计 1；环境槽抽取是闭包构造内部的
        #                    布局动作，源 Lambda 不解引用自由变量，故不计）
        # TMkCell/TFillCell/TBegin 是纯布局簿记，不单独计步（内嵌表达式照常计）。
        if count and isinstance(
            e,
            (
                ast.TConst,
                ast.TGet,
                ast.TCellGet,
                ast.TCellSet,
                ast.TPrim,
                ast.TMkClosure,
                ast.TCall,
                ast.TIf0,
                ast.TWithFrame,
            ),
        ):
            self.tick()
        if isinstance(e, ast.TConst):
            return e.value
        if isinstance(e, ast.TGet):
            return self.slot(chain, e.depth, e.slot)
        if isinstance(e, ast.TCellGet):
            cell = self.slot(chain, e.depth, e.slot)
            if not isinstance(cell, Cell):
                raise LangRuntimeError(f"expected cell at ({e.depth},{e.slot})")
            if not cell.filled:
                raise LangRuntimeError(
                    f"read of recursive slot {e.name!r} before initialization"
                )
            return cell.value
        if isinstance(e, ast.TCellSet):
            cell = self.slot(chain, e.depth, e.slot)
            # 赋值还要写 cell、产生事件：value 是普通子表达式，不是尾位置
            v = self._as_int(self.eval(e.value, chain, tail=False))
            if not isinstance(cell, Cell):
                raise LangRuntimeError(f"expected cell at ({e.depth},{e.slot})")
            cell.value = v
            cell.filled = True
            self.events.append(("set", e.name, v))
            return v
        if isinstance(e, ast.TPrim):
            if e.op != "add":
                raise LangRuntimeError(f"unknown primitive {e.op!r}")
            acc = 0
            # 加法参数还要参与归约，其中的调用都是普通调用
            for a in e.args:
                acc += self._as_int(self.eval(a, chain, tail=False))
            return acc
        if isinstance(e, ast.TMkCell):
            if e.value is None:
                return Cell(filled=False)
            return Cell(self.eval(e.value, chain, tail=False))
        if isinstance(e, ast.TFillCell):
            cell = self.slot(chain, e.depth, e.slot)
            v = self.eval(e.value, chain, tail=False)
            if not isinstance(cell, Cell) or cell.filled:
                raise LangRuntimeError(
                    f"recursive slot {e.name!r} already initialized or not a cell"
                )
            cell.value = v
            cell.filled = True
            return 0
        if isinstance(e, ast.TMkClosure):
            # 捕获槽抽取是闭包构造内部的布局复制（源 Lambda 不解引用自由变量）
            record = tuple(
                self.eval(init, chain, tail=False, count=False) for init in e.env
            )
            fn = self.program.functions.get(e.fid)
            if fn is None:
                raise LangRuntimeError(f"unknown function id {e.fid!r}")
            if len(record) != fn.n_capture:
                raise LangRuntimeError(
                    f"closure record for {e.fid} has {len(record)} slots, "
                    f"expected {fn.n_capture}"
                )
            return Closure(e.fid, record)
        if isinstance(e, ast.TCall):
            # 函数与实参严格从左到右在*调用者*环境中求值；其中的嵌套
            # 调用都是普通调用，尾位置只属于整个 TCall 自身
            callee = self.eval(e.func, chain, tail=False)
            args = [self.eval(a, chain, tail=False) for a in e.args]
            if e.tail:
                # 尾位置：本激活帧之后不再需要（被新闭包引用的槽已复制进
                # 它的捕获记录，Cell 按身份共享），交给 _drive 复用现场
                return _TailCall(callee, args)
            return self._activate(callee, args)
        if isinstance(e, ast.TIf0):
            c = self._as_int(self.eval(e.cond, chain, tail=False))
            # 尾位置穿透被选中分支；未选中分支不求值（副作用顺序不变）
            branch = e.then_e if c == 0 else e.else_e
            return self._eval_raw(branch, chain, tail=tail)
        if isinstance(e, ast.TWithFrame):
            # 严格性：inits 在外层链上全部求值后，帧才可见
            values = tuple(self.eval(init, chain, tail=False) for init in e.inits)
            # 追加的是词法帧（let/letrec 数据），不是函数调用层；
            # body 继承同一尾位置。被捕获数据活在新帧中，不受尾调用影响。
            return self._eval_raw(e.body, (values,) + chain, tail=tail)
        if isinstance(e, ast.TBegin):
            result: object = 0
            for i, sub in enumerate(e.exprs):
                is_last = i == len(e.exprs) - 1
                # 仅最后一个表达式（letrec 组体）承担外层尾位置
                result = self._eval_raw(sub, chain, tail=tail and is_last)
            return result
        raise LangRuntimeError(f"cannot execute IR node {type(e).__name__}")

    def _build_activation(self, callee: object, args: list[object]) -> tuple[ast.TFunction, tuple]:
        """校验被调用者并构造激活帧记录；非函数/元数不符在这里报错。"""
        if not isinstance(callee, Closure):
            raise LangRuntimeError("attempt to call non-function value")
        fn = self.program.functions[callee.fid]
        if len(args) != len(fn.params):
            raise LangRuntimeError(
                f"function {fn.fid} expects {len(fn.params)} args, got {len(args)}"
            )
        slots: list[object] = list(callee.record)
        for i, a in enumerate(args):
            if fn.mutable_params[i]:
                slots.append(Cell(a))
            else:
                slots.append(a)
        act_record = tuple(slots)
        if len(act_record) != fn.n_slots:
            raise LangRuntimeError(
                f"activation record for {fn.fid} has {len(act_record)} slots, "
                f"expected {fn.n_slots}"
            )
        return fn, act_record

    def _enter(self, callee: object, args: list[object]) -> tuple[ast.TExpr, tuple[tuple, ...]]:
        """尾调用入口：构造被调用者激活帧，返回其 (函数体, 新环境链)。

        不触碰 stats.depth——旧帧的层数由调用者直接被本帧顶替。"""
        fn, act_record = self._build_activation(callee, args)
        return fn.body, (act_record,)

    def _activate(self, callee: object, args: list[object]) -> object:
        """普通调用入口：嵌套一层真实调用现场（行为与尾调用消除前一致）。"""
        fn, act_record = self._build_activation(callee, args)
        self.stats.calls += 1
        self.stats.depth += 1
        if self.stats.depth > self.stats.peak_depth:
            self.stats.peak_depth = self.stats.depth
        try:
            return self.eval(fn.body, (act_record,), tail=True, owns_frame=True)
        finally:
            self.stats.depth -= 1

    def apply(self, callee: object, args: list[object]) -> object:
        """对外保留的普通调用接口（非尾调用语义）。"""
        return self._activate(callee, args)

    def _as_int(self, v: object) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise LangRuntimeError(f"integer expected, got {type(v).__name__}")
        return v

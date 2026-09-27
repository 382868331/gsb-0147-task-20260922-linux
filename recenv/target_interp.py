"""目标 IR 解释器：在显式环境布局（帧链 + 槽位 + Cell）上执行转换输出。

值只有整数、:class:`Closure`、:class:`Cell`。副作用事件与源解释器使用
完全相同的 ``("set", 名字, 整数)`` 记录格式，便于逐事件对照。

本解释器只依赖 :mod:`recenv.ast_nodes` 中的 IR 定义，不调用转换器。

尾部调用复用
------------
函数体尾位置（TIf0 分支、TBegin 末式、TWithFrame 体的尾位置）上的
TCall 不再压入新的调用层：函数与实参仍按从左到右在当前环境链上
求值，随后当前激活记录被丢弃，新激活在同一调用层上就地开始
（蹦床循环）。因此被保留的调用层数（RunStats.max_call_depth）
只随非尾调用增长；连续尾调用链的运行占用是常数。闭包捕获记录由闭包
对象自身持有，调用现场复用不会释放仍被捕获的数据；非尾调用路径完全
不变。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import LangRuntimeError, StepLimitError

# 与源解释器相同：提升栈余量，保证 10000 步预算先于递归深度报错触发。
sys.setrecursionlimit(max(100_000, sys.getrecursionlimit()))

DEFAULT_STEP_LIMIT = 10_000


@dataclass(frozen=True)
class RunStats:
    """一次目标程序执行的运行占用统计。

    Attributes:
        steps: 实际执行的计步数（与源解释器同口径）
        max_call_depth: 执行期间同时保留的最大函数调用层数
            （尾调用复用同一层，因此深层尾递归该值保持常数）
    """

    steps: int
    max_call_depth: int


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
class _TailCall:
    """尾位置调用的蹦床信号：当前激活已不需要，交由调用层循环就地复用。"""

    callee: object
    args: list


def run_program(
    program: ast.Program, step_limit: int = DEFAULT_STEP_LIMIT
) -> tuple[int, list[tuple[str, str, int]]]:
    """执行 :func:`recenv.convert.convert` 的输出。

    Returns:
        (整数结果, 副作用事件序列)
    """
    interp = _TargetInterp(program, step_limit)
    try:
        result = interp.eval(program.main, ())
    except StepLimitError as e:
        e.events = list(interp.events)
        raise
    if isinstance(result, bool) or not isinstance(result, int):
        raise LangRuntimeError(
            f"program result must be an integer, got {type(result).__name__}"
        )
    return result, interp.events


def run_program_with_stats(
    program: ast.Program, step_limit: int = DEFAULT_STEP_LIMIT
) -> tuple[int, list[tuple[str, str, int]], RunStats]:
    """与 :func:`run_program` 相同，但额外返回运行占用统计。

    统计中的 ``max_call_depth`` 是**实际保留的调用层数**，用于证明
    尾调用复用减少的是运行占用，而不是调高了递归上限。
    """
    interp = _TargetInterp(program, step_limit)
    try:
        result = interp.eval(program.main, ())
    except StepLimitError as e:
        e.events = list(interp.events)
        raise
    if isinstance(result, bool) or not isinstance(result, int):
        raise LangRuntimeError(
            f"program result must be an integer, got {type(result).__name__}"
        )
    return result, interp.events, interp.stats()


class _TargetInterp:
    def __init__(self, program: ast.Program, step_limit: int) -> None:
        self.program = program
        self.steps = 0
        self.limit = step_limit
        self.events: list[tuple[str, str, int]] = []
        self.call_depth = 0
        self.max_call_depth = 0

    def stats(self) -> RunStats:
        return RunStats(steps=self.steps, max_call_depth=self.max_call_depth)

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
            v = self._as_int(self.eval(e.value, chain))
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
            for a in e.args:
                acc += self._as_int(self.eval(a, chain))
            return acc
        if isinstance(e, ast.TMkCell):
            if e.value is None:
                return Cell(filled=False)
            return Cell(self.eval(e.value, chain))
        if isinstance(e, ast.TFillCell):
            cell = self.slot(chain, e.depth, e.slot)
            v = self.eval(e.value, chain)
            if not isinstance(cell, Cell) or cell.filled:
                raise LangRuntimeError(
                    f"recursive slot {e.name!r} already initialized or not a cell"
                )
            cell.value = v
            cell.filled = True
            return 0
        if isinstance(e, ast.TMkClosure):
            # 捕获槽抽取是闭包构造内部的布局复制（源 Lambda 不解引用自由变量）
            record = tuple(self.eval(init, chain, count=False) for init in e.env)
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
            callee = self.eval(e.func, chain)
            args = [self.eval(a, chain) for a in e.args]
            if tail:
                # 尾位置：函数/实参求值完成后当前调用现场已不再需要，
                # 返回蹦床信号，由 apply 的循环在同一调用层上复用。
                return _TailCall(callee, args)
            return self.apply(callee, args)
        if isinstance(e, ast.TIf0):
            c = self._as_int(self.eval(e.cond, chain))
            return self.eval(e.then_e if c == 0 else e.else_e, chain, tail=tail)
        if isinstance(e, ast.TWithFrame):
            # 严格性：inits 在外层链上全部求值后，帧才可见
            values = tuple(self.eval(init, chain) for init in e.inits)
            return self.eval(e.body, (values,) + chain, tail=tail)
        if isinstance(e, ast.TBegin):
            result: object = 0
            for i, sub in enumerate(e.exprs):
                if i == len(e.exprs) - 1:
                    return self.eval(sub, chain, tail=tail)
                result = self.eval(sub, chain)
            return result
        raise LangRuntimeError(f"cannot execute IR node {type(e).__name__}")

    def apply(self, callee: object, args: list[object]) -> object:
        """进入一次调用层；层内用蹦床循环复用连续的尾调用。

        尾调用的函数与实参在旧现场上求值完毕后，旧激活记录即被丢弃，
        新激活在同一层上就地绑定，因此尾调用链不增加 call_depth。
        闭包捕获记录由 Closure 对象自身持有，复用不影响仍被捕获的数据。
        """
        self.call_depth += 1
        if self.call_depth > self.max_call_depth:
            self.max_call_depth = self.call_depth
        try:
            while True:
                act_record = self._bind_activation(callee, args)
                fn = self.program.functions[callee.fid]
                result = self.eval(fn.body, (act_record,), tail=True)
                if not isinstance(result, _TailCall):
                    return result
                callee, args = result.callee, result.args
        finally:
            self.call_depth -= 1

    def _bind_activation(self, callee: object, args: list[object]) -> tuple:
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
        return act_record

    def _as_int(self, v: object) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise LangRuntimeError(f"integer expected, got {type(v).__name__}")
        return v

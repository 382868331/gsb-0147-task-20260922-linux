"""目标 IR 解释器：在显式环境布局（帧链 + 槽位 + Cell）上执行转换输出。

值只有整数、:class:`Closure`、:class:`Cell`。副作用事件与源解释器使用
完全相同的 ``("set", 名字, 整数)`` 记录格式，便于逐事件对照。

本解释器只依赖 :mod:`recenv.ast_nodes` 中的 IR 定义，不调用转换器。
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


class _TargetInterp:
    def __init__(self, program: ast.Program, step_limit: int) -> None:
        self.program = program
        self.steps = 0
        self.limit = step_limit
        self.events: list[tuple[str, str, int]] = []

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

    def eval(self, e: ast.TExpr, chain: tuple[tuple, ...], count: bool = True) -> object:
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
            return self.apply(callee, args)
        if isinstance(e, ast.TIf0):
            c = self._as_int(self.eval(e.cond, chain))
            return self.eval(e.then_e if c == 0 else e.else_e, chain)
        if isinstance(e, ast.TWithFrame):
            # 严格性：inits 在外层链上全部求值后，帧才可见
            values = tuple(self.eval(init, chain) for init in e.inits)
            return self.eval(e.body, (values,) + chain)
        if isinstance(e, ast.TBegin):
            result: object = 0
            for sub in e.exprs:
                result = self.eval(sub, chain)
            return result
        raise LangRuntimeError(f"cannot execute IR node {type(e).__name__}")

    def apply(self, callee: object, args: list[object]) -> object:
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
        return self.eval(fn.body, (act_record,))

    def _as_int(self, v: object) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise LangRuntimeError(f"integer expected, got {type(v).__name__}")
        return v

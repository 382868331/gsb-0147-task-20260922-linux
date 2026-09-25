"""目标 IR 解释器：在显式环境布局（帧链 + 槽位 + Cell）上执行转换输出。

值只有整数、:class:`Closure`、:class:`Cell`。副作用事件与源解释器使用
完全相同的 ``("set", 名字, 整数)`` 记录格式，便于逐事件对照。

本解释器只依赖 :mod:`recenv.ast_nodes` 中的 IR 定义，不调用转换器。

尾调用执行（trampoline + 显式调用帧）
--------------------------------------

``eval`` 沿 IR 语法树递归下降；语法树嵌套深度由 500 节点上限界定，与函数
被调用多少次无关，因此**不抬高** Python 递归深度限制。

* 尾位置调用（:class:`~recenv.ast_nodes.TTailCall`）：函数与全部实参在当前
  帧中严格从左到右求值完成后抛出 :class:`_TailJump`；实参求值出错时跳转
  根本不会发生，当前帧不会被提前替换。
* :meth:`_invoke` 是唯一创建调用帧的地方：它在一个循环里接住
  :class:`_TailJump`，用目标函数的激活记录**替换**当前记录（不增加 Python
  栈深度，也不增加活动调用帧计数）。
* 非尾调用（:class:`~recenv.ast_nodes.TCall`）才压入新帧：``eval`` 调用
  :meth:`_invoke`，后者 ``frames += 1`` 后再求函数体。

``call_frames`` 是当前尚未返回的非尾函数激活数（主表达式 main 不计帧），
``max_call_frames`` 记录其峰值——这是真实的活动调用帧数，不是被藏起来的
Python 递归深度。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import LangRuntimeError, StepLimitError

# 余量说明：eval 沿语法树下降（≤500 节点），*非尾*调用每激活一次会真实地
# 多压数层 Python 栈（eval -> _invoke -> eval ...）——这些激活是真实存在、
# 必须保留的调用帧，10000 步预算内最深可能嵌套数千层。这里抬栈只为容纳
# 这种真实的非尾嵌套，使步数预算先于 RecursionError 触发；它*不*参与尾调用
# ——尾调用经 _TailJump 在 _invoke 循环内迭代，Python 栈深度恒定。
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


@dataclass(frozen=True)
class RunStats:
    """一次目标执行的可观察计量。

    Attributes:
        value: 整数结果
        events: 副作用事件序列 ``("set", 名字, 整数)``
        steps: 实际执行的源语言归约步数
        max_call_frames: 峰值活动调用帧数（尚未返回的非尾函数激活数；
            主表达式不计帧；尾调用在帧内替换，不增帧）
        tail_jumps: 尾调用跳转总次数（每次跳转替换一个调用帧）
    """

    value: int
    events: list[tuple[str, str, int]]
    steps: int
    max_call_frames: int
    tail_jumps: int


class _TailJump(BaseException):
    """尾位置调用信号：函数与实参已在当前帧全部求值成功，请求替换当前帧。

    继承 BaseException 而非 Exception：它是内部控制流，绝不能被语言语义上
    的异常处理（本语言也没有异常处理）或宽泛的 except Exception 吞掉。
    """

    __slots__ = ("callee", "args", "loc")

    def __init__(self, callee: object, args: list[object], loc: str | None) -> None:
        self.callee = callee
        self.args = args
        self.loc = loc


def run_program(
    program: ast.Program, step_limit: int = DEFAULT_STEP_LIMIT
) -> tuple[int, list[tuple[str, str, int]]]:
    """执行 :func:`recenv.convert.convert` 的输出。

    Returns:
        (整数结果, 副作用事件序列)
    """
    stats = run_program_with_stats(program, step_limit)
    return stats.value, stats.events


def run_program_with_stats(
    program: ast.Program, step_limit: int = DEFAULT_STEP_LIMIT
) -> RunStats:
    """同 :func:`run_program`，额外返回步数 / 峰值调用帧 / 尾跳转计数。"""
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
    return RunStats(
        value=result,
        events=list(interp.events),
        steps=interp.steps,
        max_call_frames=interp.max_call_frames,
        tail_jumps=interp.tail_jumps,
    )


class _TargetInterp:
    def __init__(self, program: ast.Program, step_limit: int) -> None:
        self.program = program
        self.steps = 0
        self.limit = step_limit
        self.events: list[tuple[str, str, int]] = []
        # 显式调用帧计量
        self.call_frames = 0
        self.max_call_frames = 0
        self.tail_jumps = 0

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
        #   Call（尾/非尾）-> TCall/TTailCall（各计 1，跳转本身不额外计步）
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
                ast.TTailCall,
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
            v = self._as_int(self.eval(e.value, chain), e.loc)
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
                acc += self._as_int(self.eval(a, chain), e.loc)
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
            # 非尾调用：先在当前帧严格从左到右求函数与实参，再压入新调用帧。
            callee = self.eval(e.func, chain)
            args = [self.eval(a, chain) for a in e.args]
            return self._invoke(callee, args, e.loc)
        if isinstance(e, ast.TTailCall):
            # 尾调用：函数与全部实参在当前帧求值成功后才请求替换当前帧；
            # 任一求值出错都在下面两行内抛出，_TailJump 不会发生，帧不变。
            callee = self.eval(e.func, chain)
            args = [self.eval(a, chain) for a in e.args]
            raise _TailJump(callee, args, e.loc)
        if isinstance(e, ast.TIf0):
            c = self._as_int(self.eval(e.cond, chain), e.loc)
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

    # -- 显式调用帧 --------------------------------------------------------

    def _invoke(self, callee: object, args: list[object], loc: str | None) -> object:
        """执行一次非尾函数激活：压入一个调用帧。

        帧内的尾调用在同一循环里迭代完成（激活记录被替换，Python 栈深度与
        ``call_frames`` 计数都不增长），直到某个函数体正常返回。
        """
        self._frames_enter()
        try:
            while True:
                fn, act_record = self._activate(callee, args, loc)
                try:
                    return self.eval(fn.body, (act_record,))
                except _TailJump as jump:
                    # 替换当前帧：不 return、不压栈，直接以新 callee 继续循环
                    self.tail_jumps += 1
                    callee, args, loc = jump.callee, jump.args, jump.loc
        finally:
            self._frames_leave()

    def _activate(
        self, callee: object, args: list[object], loc: str | None
    ) -> tuple[ast.TFunction, tuple]:
        """校验被调值与元数，构造激活记录（捕获槽 + 参数槽）。"""
        prefix = f"{loc}: " if loc else ""
        if not isinstance(callee, Closure):
            raise LangRuntimeError(f"{prefix}attempt to call non-function value")
        fn = self.program.functions.get(callee.fid)
        if fn is None:
            raise LangRuntimeError(f"unknown function id {callee.fid!r}")
        if len(args) != len(fn.params):
            raise LangRuntimeError(
                f"{prefix}function expects {len(fn.params)} args, got {len(args)}"
            )
        slots: list[object] = list(callee.record)
        for i, a in enumerate(args):
            # 与源解释器 apply 同口径：实参只能是整数或函数（bool 拒绝）
            self._check_arg(a, loc)
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

    def _frames_enter(self) -> None:
        self.call_frames += 1
        if self.call_frames > self.max_call_frames:
            self.max_call_frames = self.call_frames

    def _frames_leave(self) -> None:
        self.call_frames -= 1

    @staticmethod
    def _prefix(loc: str | None) -> str:
        return f"{loc}: " if loc else ""

    @staticmethod
    def _type_name(v: object) -> str:
        """语言层面的类型名（与源解释器一致：integer / function / cell）。"""
        if isinstance(v, bool):
            return "boolean"
        if isinstance(v, int):
            return "integer"
        if isinstance(v, Closure):
            return "function"
        if isinstance(v, Cell):
            return "cell"
        return type(v).__name__

    def _as_int(self, v: object, loc: str | None = None) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise LangRuntimeError(
                f"{self._prefix(loc)}integer expected, got {self._type_name(v)}"
            )
        return v

    def _check_arg(self, v: object, loc: str | None) -> None:
        if isinstance(v, bool) or not isinstance(v, (int, Closure)):
            raise LangRuntimeError(
                f"{self._prefix(loc)}bad argument value: {self._type_name(v)}"
            )

"""目标 IR 解释器：显式调用帧 + 显式续体栈的迭代求值机。

本解释器**不使用 Python 调用栈表达语言调用**，执行期间 Python 递归深度
恒为小常数，因此尾递归/互递归可以跑到远超宿主递归深度的步数。

运行状态：唯一的状态核心是显式续体栈 ``stack``。栈中 ``("ret", ...)``
与 ``("ret_root", ...)`` 是**调用帧边界标记**；标记数量即当前存活的
函数调用帧数，``max_active_frames`` 记录其峰值。

调用语义
--------

* 非尾位置 :class:`~recenv.ast_nodes.TCall`：函数位置与参数严格先函数后
  参数、从左到右全部求值成功，并通过 callee/元数校验后，压入新的
  ``("ret",)`` 帧标记（活动帧数 +1）并跳转到函数体。
* 尾位置 :class:`~recenv.ast_nodes.TTailCall`：函数与参数同样全部求值、
  校验通过、新激活记录构造完成后，丢弃当前帧边界及其上的残留续体，
  **替换**为同种类的新边界并跳转——活动调用帧数不增加（计入
  ``tail_jumps``）。参数求值或校验失败时帧尚未替换，异常直接传播。
* 主表达式尾位置直接进入的第一个函数帧用 ``("ret_root",)`` 标记：
  它返回时其值即程序结果；它内部再尾调用时同样原地替换，帧数不增长。
  非尾调用则在其上叠加普通 ``("ret",)`` 标记，返回时逐层弹出。

词法环境仍沿用闭包转换后的显式记录布局：函数激活时环境链只含本帧
``(record,)``（自由变量已扁平进捕获槽）；``TWithFrame`` 在链顶追加记录，
链快照保存在调用方续体中，函数返回/尾跳转时随帧释放。互递归组共享
cell、闭包共享 cell 的运行期语义与旧递归下降实现完全一致。

值只有整数、:class:`Closure`、:class:`Cell`。副作用事件与源解释器使用
完全相同的 ``("set", 名字, 整数)`` 记录格式，便于逐事件对照。

本解释器只依赖 :mod:`recenv.ast_nodes` 中的 IR 定义，不调用转换器。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ast_nodes as ast
from .errors import LangRuntimeError, StepLimitError

DEFAULT_STEP_LIMIT = 10_000

# 续体处理结果：继续派发下一个表达式，或直接产出一个值向栈底传递
_GO = ("go",)
_VALUE = ("value",)

# _reduce 对需要续体的复合节点返回此哨兵，并把下一个 (expr, chain) 放在 self._next
_CONT = object()


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
    """一次目标执行的完整统计。

    Attributes:
        result: 程序整数结果
        events: 副作用事件序列（与源解释器同口径）
        steps: 实际执行的求值步数
        max_active_frames: 执行期间同时存活的最大**函数**调用帧数
            （不含主表达式哨兵帧；尾跳转不增加此值）
        tail_jumps: 尾调用替换当前帧的次数
    """

    result: int
    events: list[tuple[str, str, int]]
    steps: int
    max_active_frames: int
    tail_jumps: int


# 计步口径与源解释器一一对应：每个源语言归约步计 1。
#   Let/LetRec 节点 -> TWithFrame（计 1）
#   Lambda 求值    -> TMkClosure（计 1；环境槽抽取是闭包构造内部的
#                    布局动作，源 Lambda 不解引用自由变量，故不计）
#   源 Call        -> TCall / TTailCall（均计 1）
# TMkCell/TFillCell/TBegin 是纯布局簿记，不单独计步（内嵌表达式照常计）。
_COUNTED = (
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
)


def run_program(
    program: ast.Program, step_limit: int = DEFAULT_STEP_LIMIT
) -> tuple[int, list[tuple[str, str, int]]]:
    """执行 :func:`recenv.convert.convert` 的输出。

    Returns:
        (整数结果, 副作用事件序列)
    """

    stats = run_program_stats(program, step_limit)
    return stats.result, stats.events


def run_program_stats(
    program: ast.Program, step_limit: int = DEFAULT_STEP_LIMIT
) -> RunStats:
    """同 :func:`run_program`，额外返回最大活动调用帧数与尾跳转次数。

    该统计是真实的显式调用帧计数：执行器不依赖 Python 递归，
    ``max_active_frames`` 由调用帧栈的实际最大长度给出。
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
    return RunStats(
        result=result,
        events=list(interp.events),
        steps=interp.steps,
        max_active_frames=interp.max_active_frames,
        tail_jumps=interp.tail_jumps,
    )


class _TargetInterp:
    def __init__(self, program: ast.Program, step_limit: int) -> None:
        self.program = program
        self.steps = 0
        self.limit = step_limit
        self.events: list[tuple[str, str, int]] = []
        self.max_active_frames = 0
        self.tail_jumps = 0
        # 续体栈中 ("ret",) 帧标记的当前数量 = 活动函数调用帧数
        self._nframes = 0
        # 驱动循环期间为 True：此时 eval 是“单节点归约”；外部直接调用
        # eval(e, chain) 则跑完整迭代驱动（保持与旧解释器相同的入口契约）。
        self._driving = False
        self._next: tuple[ast.TExpr, tuple[tuple, ...]] | None = None
        # 显式续体栈；("ret",) 帧边界的数量即当前活动函数调用帧数。
        self.stack: list[tuple] = []

    def tick(self) -> None:
        self.steps += 1
        if self.steps > self.limit:
            raise StepLimitError(self.steps, self.limit)

    def active_frames(self) -> int:
        """当前存活的函数调用帧数（续体栈中帧边界标记的数量）。"""
        return self._nframes

    # -- 入口 --------------------------------------------------------------

    def eval(self, e: ast.TExpr, chain: tuple[tuple, ...]) -> object:
        """驱动入口 / 单节点归约（取决于 self._driving）。

        * 驱动循环外调用（含旧测试中的 ``interp.eval(main, ())`` 用法）：
          从 ``e`` 跑到完整结果，初始化全新的续体栈；
        * 驱动循环内调用：只归约当前节点。复合节点压入续体并返回
          :data:`_CONT`，下一派发点记录在 ``self._next``。

        计步在驱动循环中按节点类型统一进行；``TMkClosure`` 的环境槽
        抽取通过本方法直接求 ``TGet``（不经循环），因此天然不计步。
        """

        if not self._driving:
            return self._drive(e, chain)
        return self._reduce(e, chain)

    def _drive(self, entry: ast.TExpr, entry_chain: tuple[tuple, ...]) -> object:
        self.stack = []
        expr: ast.TExpr = entry
        chain: tuple[tuple, ...] = entry_chain
        self._driving = True
        try:
            while True:
                if isinstance(expr, _COUNTED):
                    self.tick()
                v = self.eval(expr, chain)
                if v is _CONT:
                    expr, chain = self._next  # type: ignore[assignment]
                    continue
                # 值回传：沿续体栈向下交付。
                while True:
                    if not self.stack:
                        return v
                    top = self.stack[-1][0]
                    if top == "ret":
                        # 普通函数帧结束：帧数 -1，值交给调用者的等待续体
                        self.stack.pop()
                        self._nframes -= 1
                        continue
                    if top == "ret_root":
                        # 根函数帧（主表达式尾调用进入的第一个函数）结束：
                        # 它的结果即程序结果
                        self.stack.pop()
                        self._nframes -= 1
                        return v
                    k = self.stack.pop()
                    tag, payload = self._resume(k, v)
                    if tag is _GO:
                        expr, chain = payload
                        break
                    v = payload
        finally:
            self._driving = False

    # -- 环境槽 ------------------------------------------------------------

    def slot(self, chain: tuple[tuple, ...], depth: int, slot: int):
        if depth >= len(chain):
            raise LangRuntimeError(f"internal error: bad environment depth {depth}")
        record = chain[depth]
        if slot >= len(record):
            raise LangRuntimeError(f"internal error: bad environment slot {slot}")
        return record[slot]

    # -- 单节点归约 --------------------------------------------------------

    def _reduce(self, e: ast.TExpr, chain: tuple[tuple, ...]) -> object:
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
            # 与递归下降实现相同的异常优先级：先用 depth/slot 定位（越界即
            # internal error），再求赋值表达式，值回传后才校验槽中是 Cell。
            cell = self.slot(chain, e.depth, e.slot)
            self.stack.append(("set", (cell, e.name, e.loc)))
            self._next = (e.value, chain)
            return _CONT
        if isinstance(e, ast.TPrim):
            if e.op != "add":
                raise LangRuntimeError(f"unknown primitive {e.op!r}")
            if not e.args:  # 转换器只产生 2 参 add；空参按 0 处理
                return 0
            self.stack.append(("prim", (e.op, [], 0, e.args, chain, e.loc)))
            self._next = (e.args[0], chain)
            return _CONT
        if isinstance(e, ast.TMkCell):
            if e.value is None:
                return Cell(filled=False)
            self.stack.append(("mkcell", None))
            self._next = (e.value, chain)
            return _CONT
        if isinstance(e, ast.TFillCell):
            cell = self.slot(chain, e.depth, e.slot)
            self.stack.append(("fill", (cell, e.name)))
            self._next = (e.value, chain)
            return _CONT
        if isinstance(e, ast.TMkClosure):
            # 捕获槽抽取是闭包构造内部的布局复制（源 Lambda 不解引用
            # 自由变量）：这里直接归约 TGet（不经驱动循环），不计步。
            record = tuple(self.eval(init, chain) for init in e.env)
            fn = self.program.functions.get(e.fid)
            if fn is None:
                raise LangRuntimeError(f"unknown function id {e.fid!r}")
            if len(record) != fn.n_capture:
                raise LangRuntimeError(
                    f"closure record for {e.fid} has {len(record)} slots, "
                    f"expected {fn.n_capture}"
                )
            return Closure(e.fid, record)
        if isinstance(e, (ast.TCall, ast.TTailCall)):
            kind = "tail" if isinstance(e, ast.TTailCall) else "call"
            # 函数位置先于参数；先压 callee 续体，再派发函数表达式
            self.stack.append(("callee", (e.args, chain, kind, e.loc)))
            self._next = (e.func, chain)
            return _CONT
        if isinstance(e, ast.TIf0):
            self.stack.append(("if", (e.then_e, e.else_e, chain, e.loc)))
            self._next = (e.cond, chain)
            return _CONT
        if isinstance(e, ast.TWithFrame):
            # 严格性：inits 全部在外层链上求值后，帧才对 body 可见
            if e.inits:
                self.stack.append(
                    ("wf", (e.inits, 0, [], e.body, chain))
                )
                self._next = (e.inits[0], chain)
            else:
                self._next = (e.body, chain)
            return _CONT
        if isinstance(e, ast.TBegin):
            # 前面的填充表达式顺序求值、值丢弃；最后一个表达式直接成为
            # begin 的结果（不留续体），尾位置因此能透传到它。
            if not e.exprs:  # 转换器不产生空 begin；与旧实现一致返回 0
                return 0
            if len(e.exprs) <= 1:
                self._next = (e.exprs[0], chain)
            else:
                self.stack.append(("seq", (e.exprs, 0, chain)))
                self._next = (e.exprs[0], chain)
            return _CONT
        raise LangRuntimeError(f"cannot execute IR node {type(e).__name__}")

    # -- 续体回传 ----------------------------------------------------------

    def _resume(self, k: tuple, v: object) -> tuple:
        kind, data = k
        if kind == "set":
            cell, name, loc = data
            iv = self._as_int(v, loc)
            if not isinstance(cell, Cell):
                # 转换器保证 set 的目标是 cell；此路径仅在手工构造 IR 时可达
                raise LangRuntimeError(f"{self._pfx(loc)}expected cell for set {name!r}")
            cell.value = iv
            cell.filled = True
            self.events.append(("set", name, iv))
            return _VALUE, iv
        if kind == "fill":
            cell, name = data
            if not isinstance(cell, Cell) or cell.filled:
                raise LangRuntimeError(
                    f"recursive slot {name!r} already initialized or not a cell"
                )
            cell.value = v
            cell.filled = True
            return _VALUE, 0
        if kind == "mkcell":
            return _VALUE, Cell(v)
        if kind == "prim":
            op, done, i, args, chain, loc = data
            done.append(self._as_int(v, loc))
            i += 1
            if i < len(args):
                self.stack.append(("prim", (op, done, i, args, chain, loc)))
                return _GO, (args[i], chain)
            acc = 0
            for x in done:
                acc += x
            return _VALUE, acc
        if kind == "callee":
            args, chain, call_kind, loc = data
            if args:
                self.stack.append(("args", (v, [], 0, args, chain, call_kind, loc)))
                return _GO, (args[0], chain)
            return self._enter(call_kind, v, [], loc)
        if kind == "args":
            callee, done, i, args, chain, call_kind, loc = data
            done.append(v)
            i += 1
            if i < len(args):
                self.stack.append(
                    ("args", (callee, done, i, args, chain, call_kind, loc))
                )
                return _GO, (args[i], chain)
            return self._enter(call_kind, callee, done, loc)
        if kind == "if":
            then_e, else_e, chain, loc = data
            self._as_int(v, loc)
            return _GO, (then_e if v == 0 else else_e, chain)
        if kind == "wf":
            inits, i, done, body, outer = data
            done.append(v)
            i += 1
            if i < len(inits):
                self.stack.append(("wf", (inits, i, done, body, outer)))
                return _GO, (inits[i], outer)
            new_chain = (tuple(done),) + outer
            return _GO, (body, new_chain)
        if kind == "seq":
            exprs, i, chain = data
            i += 1
            if i < len(exprs) - 1:
                self.stack.append(("seq", (exprs, i, chain)))
                return _GO, (exprs[i], chain)
            # 最后一个表达式直接派发，不挂续体
            return _GO, (exprs[-1], chain)
        raise LangRuntimeError(f"internal error: bad continuation {kind!r}")

    # -- 调用帧管理 --------------------------------------------------------

    def _bump_frames(self) -> None:
        self._nframes += 1
        if self._nframes > self.max_active_frames:
            self.max_active_frames = self._nframes

    def _enter(self, kind: str, callee: object, args: list[object], loc: str | None) -> tuple:
        """函数与全部参数从左到右求值完成后的调用入口。

        校验失败（非函数 / 元数不符 / 记录尺寸不符）时直接抛错：
        非尾调用尚未压帧标记，尾调用尚未丢弃/替换当前帧标记，
        即“参数求值出错不能提前替换帧”。
        """

        pfx = self._pfx(loc)
        if not isinstance(callee, Closure):
            raise LangRuntimeError(f"{pfx}attempt to call non-function value")
        fn = self.program.functions[callee.fid]
        if len(args) != len(fn.params):
            raise LangRuntimeError(
                f"{pfx}function expects {len(fn.params)} args, got {len(args)}"
            )
        slots: list[object] = list(callee.record)
        for i, a in enumerate(args):
            if fn.mutable_params[i]:
                slots.append(Cell(a))
            else:
                slots.append(a)
        record = tuple(slots)
        if len(record) != fn.n_slots:
            raise LangRuntimeError(
                f"activation record for {fn.fid} has {len(record)} slots, "
                f"expected {fn.n_slots}"
            )

        if kind == "call":
            # 非尾调用：在调用者等待续体之上压入新帧边界。此刻 func/args
            # 的续体均已弹出，故边界之上在跳转到函数体前没有残留续体。
            self.stack.append(("ret", None))
            self._bump_frames()
        else:
            # 尾调用：找到当前帧的边界标记（从栈顶向下第一个 ret/ret_root），
            # 丢弃它及其上的当前帧残留续体，压入同种类的新边界后跳转。
            j = None
            for idx in range(len(self.stack) - 1, -1, -1):
                if self.stack[idx][0] in ("ret", "ret_root"):
                    j = idx
                    break
            if j is None:
                # 主表达式尾位置直接发起的调用：此前没有任何函数帧。
                # 按不变量此刻栈应为空；保守清空后压入根帧标记。
                self.stack.clear()
                self.stack.append(("ret_root", None))
                self._bump_frames()
            else:
                marker = self.stack[j][0]
                del self.stack[j:]
                self.stack.append((marker, None))
                # 标记数量不变：替换当前帧，活动函数帧数不增加
            self.tail_jumps += 1
        # 新激活的环境链只含本帧（捕获已扁平化进 record）
        return _GO, (fn.body, (record,))

    @staticmethod
    def _pfx(loc: str | None) -> str:
        return f"{loc}: " if loc else ""

    def _as_int(self, v: object, loc: str | None = None) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise LangRuntimeError(
                f"{self._pfx(loc)}integer expected, got {type(v).__name__}"
            )
        return v

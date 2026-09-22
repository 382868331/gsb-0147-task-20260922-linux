"""目标 IR 解释器（显式环境、显式步数预算）。

采用显式栈抽象机（CEK 风格）而非 Python 递归，使 10000 步预算内
任意嵌套深度的调用都能执行。求值顺序严格从左到右。

值只有四类：Python ``int``、闭包 ``_Closure(fid, env)``、共享组帧
``_GroupFrame``（槽位可以是未初始化哨兵）、可变单元 ``_Cell``。

控制栈元素均为元组指令；每条被处理的指令记一次执行步，超过
``max_steps``（默认 10000）抛 :class:`StepLimitError` —— 预算耗尽，
区别于已确定的程序错误。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ir
from .errors import (
    ArityError,
    LangTypeError,
    StepLimitError,
    UninitializedSlotError,
)

DEFAULT_STEP_LIMIT = 10_000
_UNINIT = object()
# 非整数赋值在副作用轨迹中的统一标记（源/目标两侧解释器共用同一常量）。
NONINT_TRACE = -(10**100)


@dataclass(slots=True)
class _Closure:
    fid: str
    env: tuple


@dataclass(slots=True)
class _Cell:
    val: object


@dataclass(slots=True)
class _GroupFrame:
    slots: list


@dataclass(slots=True)
class _Frame:
    fid: str
    env: tuple
    locals: list


@dataclass(slots=True)
class Result:
    value: int
    """程序返回值（本语言顶层结果必为整数）。"""

    steps: int
    """实际执行步数。"""

    trace: list[tuple[int, int]]
    """赋值副作用序列：(set 表达式编号, 值标记)，按发生顺序。

    整数直接记录；闭包/组帧等非整数赋值记 :data:`NONINT_TRACE`。
    """


def run(program: ir.Program, *, max_steps: int = DEFAULT_STEP_LIMIT) -> Result:
    """执行 convert 的输出。"""
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) \
            or max_steps <= 0:
        raise ValueError("max_steps 必须是正整数")

    main = program.functions[program.main_fid]
    frames: list[_Frame] = [_Frame(main.fid, (), [])]
    ctrl: list = [("eval", main.body)]
    vals: list = []
    trace: list[tuple[int, int]] = []
    steps = 0

    while ctrl:
        if steps >= max_steps:
            raise StepLimitError(
                f"执行步数超过预算 {max_steps}（已执行 {steps} 步）；"
                "这是预算耗尽，不代表程序无解或无界")
        insn = ctrl.pop()
        steps += 1
        op = insn[0]
        fr = frames[-1]

        if op == "eval":
            _eval(insn[1], ctrl, fr, vals, frames, program)
        elif op == "add":
            r = _pop_int(vals, "+ 右操作数")
            l = _pop_int(vals, "+ 左操作数")
            vals.append(l + r)
        elif op == "mkcell":
            vals.append(_Cell(vals.pop()))
        elif op == "cellget":
            c = vals.pop()
            if not isinstance(c, _Cell):
                raise LangTypeError("期望可变 cell")  # pragma: no cover
            vals.append(c.val)
        elif op == "cellset":
            tag = insn[1]
            v = vals.pop()
            c = vals.pop()
            if not isinstance(c, _Cell):
                raise LangTypeError("set 目标不是可变 cell")
            c.val = v
            _record(trace, tag, v)
            vals.append(v)
        elif op == "setlocal":
            _, idx, tag = insn
            v = vals.pop()
            fr.locals[idx] = v
            _record(trace, tag, v)
            vals.append(v)
        elif op == "setfree":
            _, slot, tag = insn
            v = vals.pop()
            c = fr.env[slot]
            if not isinstance(c, _Cell):
                raise LangTypeError("自由变量 set 目标不是 cell")
            c.val = v
            _record(trace, tag, v)
            vals.append(v)
        elif op == "setgroup":
            _, index, tag = insn
            v = vals.pop()
            fr.env[0].slots[index] = v
            _record(trace, tag, v)
            vals.append(v)
        elif op == "frameset":
            _, index, tag = insn
            v = vals.pop()
            gf = vals.pop()
            if not isinstance(gf, _GroupFrame):
                raise LangTypeError("组帧 set 目标不是组帧")  # pragma: no cover
            gf.slots[index] = v
            _record(trace, tag, v)
            vals.append(v)
        elif op == "frameget":
            _, index, bid = insn
            gf = vals.pop()
            if not isinstance(gf, _GroupFrame):
                raise LangTypeError("期望组帧")  # pragma: no cover
            v = gf.slots[index]
            if v is _UNINIT:
                raise UninitializedSlotError(
                    f"访问了尚未初始化的组帧槽（绑定 {bid}）",
                    during_initialization=True)
            vals.append(v)
        elif op == "if":
            c = _pop_int(vals, "if0 条件")
            ctrl.append(("eval", insn[1] if c == 0 else insn[2]))
        elif op == "letpush":
            fr.locals.append(vals.pop())
        elif op == "letpop":
            fr.locals.pop()
        elif op == "fillwrite":
            _, frame_slot, slot = insn
            v = vals.pop()
            gf = fr.locals[frame_slot]
            if not isinstance(gf, _GroupFrame) or not isinstance(v, _Closure):
                raise LangTypeError("letrec 组帧填充类型错误")  # pragma: no cover
            gf.slots[slot] = v
        elif op == "mkclos":
            _, fid, nenv = insn
            env = tuple(reversed([vals.pop() for _ in range(nenv)]))
            vals.append(_Closure(fid, env))
        elif op == "call":
            nargs = insn[1]
            args = [vals.pop() for _ in range(nargs)]
            args.reverse()
            fval = vals.pop()
            if not isinstance(fval, _Closure):
                raise LangTypeError("调用对象不是函数")
            fn = program.functions.get(fval.fid)
            if fn is None:  # pragma: no cover
                raise LangTypeError(f"函数表中不存在 {fval.fid}")
            if len(args) != fn.nparams:
                raise ArityError(
                    f"函数 {fn.name!r} 需要 {fn.nparams} 个实参，"
                    f"得到 {len(args)} 个")
            frames.append(_Frame(fval.fid, fval.env, list(args)))
            ctrl.append(("return",))
            ctrl.append(("eval", fn.body))
        elif op == "return":
            frames.pop()
        else:  # pragma: no cover
            raise AssertionError(f"未知指令 {op}")

    if len(vals) != 1:  # pragma: no cover
        raise AssertionError("机器终止时值栈异常")
    result = vals[0]
    if isinstance(result, bool) or not isinstance(result, int):
        raise LangTypeError(
            f"程序顶层结果必须是整数，得到 {_type_name(result)}")
    return Result(value=result, steps=steps, trace=trace)


def _pop_int(vals: list, what: str) -> int:
    v = vals.pop()
    if isinstance(v, bool) or not isinstance(v, int):
        raise LangTypeError(f"{what}不是整数")
    return v


def _record(trace: list, tag: int, v: object) -> None:
    if isinstance(v, bool) or not isinstance(v, int):
        trace.append((tag, NONINT_TRACE))
    else:
        trace.append((tag, v))


def _type_name(v: object) -> str:
    if isinstance(v, _Closure):
        return f"closure({v.fid})"
    if isinstance(v, _Cell):
        return "cell"
    if isinstance(v, _GroupFrame):
        return "group-frame"
    return type(v).__name__


# --------------------------------------------------------------- 表达式分派

def _eval(node: ir.TNode, ctrl: list, fr: _Frame, vals: list,
          frames: list, program: ir.Program) -> None:
    push = ctrl.append

    if isinstance(node, ir.TInt):
        vals.append(node.value)
    elif isinstance(node, ir.TAdd):
        push(("add",))
        push(("eval", node.rhs))
        push(("eval", node.lhs))
    elif isinstance(node, ir.TLocal):
        vals.append(fr.locals[node.index])
    elif isinstance(node, ir.TGet):
        vals.append(fr.env[node.slot])
    elif isinstance(node, ir.TGetGroup):
        v = fr.env[0].slots[node.index]
        if v is _UNINIT:
            raise UninitializedSlotError(
                f"访问了尚未初始化的组帧槽（绑定 {node.bid}）",
                during_initialization=True)
        vals.append(v)
    elif isinstance(node, ir.TFrameGet):
        push(("frameget", node.index, node.bid))
        push(("eval", node.frame))
    elif isinstance(node, ir.TClosure):
        push(("mkclos", node.fid, len(node.env)))
        # env[0] 先求值（严格从左到右）：逆序压栈。
        for e in reversed(node.env):
            push(("eval", e))
    elif isinstance(node, ir.TCell):
        push(("mkcell",))
        push(("eval", node.value))
    elif isinstance(node, ir.TCellGet):
        push(("cellget",))
        push(("eval", node.cell))
    elif isinstance(node, ir.TCellSet):
        push(("cellset", node.tag))
        push(("eval", node.value))
        push(("eval", node.cell))
    elif isinstance(node, ir.TSetLocal):
        push(("setlocal", node.index, node.tag))
        push(("eval", node.value))
    elif isinstance(node, ir.TSetFree):
        push(("setfree", node.slot, node.tag))
        push(("eval", node.value))
    elif isinstance(node, ir.TSetGroup):
        push(("setgroup", node.index, node.tag))
        push(("eval", node.value))
    elif isinstance(node, ir.TFrameSet):
        # frame 子表达式在左、先求值。
        push(("frameset", node.index, node.tag))
        push(("eval", node.value))
        push(("eval", node.frame))
    elif isinstance(node, ir.TCall):
        push(("call", len(node.args)))
        for a in reversed(node.args):
            push(("eval", a))
        push(("eval", node.func))
    elif isinstance(node, ir.TIf0):
        push(("if", node.then, node.els))
        push(("eval", node.cond))
    elif isinstance(node, ir.TLet):
        push(("letpop",))
        push(("eval", node.body))
        push(("letpush",))
        push(("eval", node.bound))
    elif isinstance(node, ir.TMakeGroup):
        vals.append(_GroupFrame([_UNINIT] * node.size))
    elif isinstance(node, ir.TGroupFill):
        # 求成员闭包值 -> 写入组帧 -> 求 body。
        push(("eval", node.body))
        push(("fillwrite", node.frame_slot, node.slot))
        push(("eval", node.value))
    else:  # pragma: no cover
        raise AssertionError(f"未知 IR 节点 {type(node).__name__}")

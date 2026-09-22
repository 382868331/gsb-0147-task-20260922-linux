"""错误类型与错误语义。

所有库自身抛出的错误均继承 :class:`ClosureLayoutError`。错误的
``message`` 末尾可携带 ``at line L column C`` 形式的定位信息，
``line`` / ``column`` 属性在可用时给出 1 起始的源码位置；对于以
AST 节点对象直接构造的程序，位置不可用，二者均为 ``None``。
"""

from __future__ import annotations


class ClosureLayoutError(Exception):
    """本库所有错误的基类。"""

    kind = "closure-layout-error"

    def __init__(self, message: str, line: int | None = None,
                 column: int | None = None) -> None:
        self.message = message
        self.line = line
        self.column = column
        if line is not None and column is not None:
            full = f"{message} (at line {line} column {column})"
        else:
            full = message
        super().__init__(full)


class ValidationError(ClosureLayoutError):
    """AST 形状/类型非法，或字面量越界。"""

    kind = "validation-error"


class NodeLimitError(ClosureLayoutError):
    """源 AST 节点数超过 500。"""

    kind = "node-limit-error"


class UnboundVariableError(ClosureLayoutError):
    """引用了未绑定的变量，或访问尚未初始化的 letrec 槽位。

    访问 letrec 初始化阶段尚未赋值的槽位属于运行期“未绑定”，
    同样使用本错误（``during_initialization`` 为 True）。
    """

    kind = "unbound-variable-error"

    def __init__(self, message: str, line: int | None = None,
                 column: int | None = None, *,
                 during_initialization: bool = False) -> None:
        super().__init__(message, line, column)
        self.during_initialization = during_initialization


class LetrecFormError(ValidationError):
    """letrec 绑定项不是 ``(name (lambda (params) body))`` 形式。"""

    kind = "letrec-form-error"


class LangTypeError(ClosureLayoutError):
    """运行期类型错误：对非整数相加、以非函数为调用对象、if0 非整数等。"""

    kind = "lang-type-error"


class ArityError(ClosureLayoutError):
    """调用实参个数与函数形参个数不符。"""

    kind = "arity-error"


class StepLimitError(ClosureLayoutError):
    """解释器执行步数超过 10000 预算。

    这是“预算耗尽”，区别于已证明的程序错误（未绑定变量等）。
    """

    kind = "step-limit-error"


class UninitializedSlotError(UnboundVariableError):
    """目标解释器中读取了尚未初始化的环境槽位。"""

    kind = "uninitialized-slot-error"

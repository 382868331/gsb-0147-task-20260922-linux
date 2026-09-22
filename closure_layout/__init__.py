"""closure_layout: 递归闭包的环境布局转换 (可复用题库)。

公开接口见 :mod:`closure_layout.api`。
"""

from .errors import (
    ClosureLayoutError,
    ValidationError,
    NodeLimitError,
    UnboundVariableError,
    LetrecFormError,
    LangTypeError,
    ArityError,
    StepLimitError,
    UninitializedSlotError,
)
from .ast import (
    int_,
    add_,
    var_,
    set_,
    if0_,
    lambda_,
    call_,
    let_,
    letrec_,
    validate,
    count_nodes,
)
from .api import (
    parse,
    convert,
    run_source,
    run_target,
    run,
    Program,
    Function,
)

__all__ = [
    "parse",
    "convert",
    "run_source",
    "run_target",
    "run",
    "Program",
    "Function",
    "int_",
    "add_",
    "var_",
    "set_",
    "if0_",
    "lambda_",
    "call_",
    "let_",
    "letrec_",
    "validate",
    "count_nodes",
    "ClosureLayoutError",
    "ValidationError",
    "NodeLimitError",
    "UnboundVariableError",
    "LetrecFormError",
    "LangTypeError",
    "ArityError",
    "StepLimitError",
    "UninitializedSlotError",
]

"""recenv —— 递归闭包的环境布局转换（闭包转换 + 显式环境）。

公开接口::

    convert(ast) -> Program                 # 核心转换
    source_interp.evaluate(ast) -> (int, events)   # 独立源解释器
    target_interp.run_program(program) -> (int, events)  # 目标 IR 解释器
    ast_nodes: 源 AST 节点与目标 IR 数据结构
    errors: 错误类型

输入上限：源 AST 最多 500 个节点；解释器默认 10000 个求值步。
"""

from . import ast_nodes, errors
from .ast_nodes import (
    Add,
    Call,
    If0,
    Int,
    Lambda,
    Let,
    LetRec,
    Program,
    RecBinding,
    Set,
    Var,
)
from .convert import MAIN_ID, convert
from .source_interp import evaluate as eval_source
from .target_interp import (
    FrameStats,
    run_program as eval_target,
    run_program_tracked as eval_target_tracked,
)

MAX_NODES = 500
DEFAULT_STEP_LIMIT = 10_000

__all__ = [
    "ast_nodes",
    "errors",
    "convert",
    "eval_source",
    "eval_target",
    "eval_target_tracked",
    "FrameStats",
    "MAIN_ID",
    "MAX_NODES",
    "DEFAULT_STEP_LIMIT",
    "Program",
    "Int",
    "Var",
    "Add",
    "Let",
    "Lambda",
    "Call",
    "If0",
    "Set",
    "RecBinding",
    "LetRec",
]

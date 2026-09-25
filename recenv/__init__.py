"""recenv —— 递归闭包的环境布局转换（闭包转换 + 显式环境 + 尾调用跳转）。

公开接口::

    convert(ast) -> Program                 # 核心转换（标记真尾位置）
    source_interp.evaluate(ast) -> (int, events)   # 独立源解释器（递归下降，保留）
    target_interp.run_program(program) -> (int, events)  # 目标 IR 解释器（迭代 CEK）
    target_interp.run_program_stats(program) -> RunStats
        # 额外给最大活动调用帧数 max_active_frames 与尾跳转次数 tail_jumps
    ast_nodes: 源 AST 节点与目标 IR 数据结构
    errors: 错误类型

输入上限：源 AST 最多 500 个节点；解释器默认 10000 个求值步。

目标解释器用显式调用帧管理非尾调用、尾调用替换当前帧，不借助 Python
递归，因此交替尾递归可以跑到远超宿主递归深度而帧数保持有界。
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
    TCall,
    TTailCall,
    Var,
)
from .convert import MAIN_ID, convert
from .source_interp import evaluate as eval_source
from .target_interp import RunStats, run_program as eval_target
from .target_interp import run_program_stats as eval_target_stats

MAX_NODES = 500
DEFAULT_STEP_LIMIT = 10_000

__all__ = [
    "ast_nodes",
    "errors",
    "convert",
    "eval_source",
    "eval_target",
    "eval_target_stats",
    "RunStats",
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
    "TCall",
    "TTailCall",
]

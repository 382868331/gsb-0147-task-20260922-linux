"""recenv —— 递归闭包的环境布局转换（闭包转换 + 显式环境）。

公开接口::

    convert(ast) -> Program                       # 核心转换（v1，行为不变）
    source_interp.evaluate(ast) -> (int, events)  # 独立源解释器（参考）
    target_interp.run_program(program) -> (int, events)  # 旧目标解释器（参考）
    machine.Machine                               # 显式栈执行机（可暂停/恢复/升级）
    image.dump_machine/load_machine/save_image/load_image  # 执行镜像
    migration.fieldize / upgrade_at_safepoint     # 结构化字段与版本迁移

输入上限：源 AST 最多 500 个节点；解释器默认 10_000 个求值步。
新能力（持久化/升级）使用独立格式 ``recenv-image/1``，旧接口完全保留。
"""

from . import ast_nodes, codetable, errors, heap, image, ircode, machine, migration, persist
from .ast_nodes import (
    Add,
    Call,
    FieldSpec,
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
from .image import (
    FORMAT as IMAGE_FORMAT,
    dump_machine,
    from_json as load_image_json,
    load_image,
    load_machine,
    save_image,
    to_json as dump_image_json,
)
from .machine import Frame, Machine, run_machine
from .migration import fieldize, upgrade_at_safepoint
from .source_interp import evaluate as eval_source
from .target_interp import run_program as eval_target

MAX_NODES = 500
DEFAULT_STEP_LIMIT = 10_000

__all__ = [
    "ast_nodes",
    "errors",
    "convert",
    "eval_source",
    "eval_target",
    # 显式栈执行机
    "Machine",
    "Frame",
    "run_machine",
    # 镜像
    "image",
    "ircode",
    "heap",
    "codetable",
    "migration",
    "persist",
    "dump_machine",
    "load_machine",
    "save_image",
    "load_image",
    "dump_image_json",
    "load_image_json",
    "IMAGE_FORMAT",
    # 字段 / 版本
    "fieldize",
    "upgrade_at_safepoint",
    "FieldSpec",
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

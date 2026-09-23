"""持久化编排：镜像文件 + 真正的新进程恢复。

* :func:`save_initial_image` 把程序字段化后保存初始（尚未归约）执行机；
* :func:`run_segment` 启动一个*独立 Python 进程*
  （``python -m recenv._worker``）载入镜像、跑一段步数预算；成功后用新
  镜像覆盖检查点文件，可直接再跑下一段；
* 父进程不向子进程传递任何运行态对象——别名/共享/环只通过镜像中的
  稳定 OID 跨越进程边界。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from . import ast_nodes as ast
from .convert import convert
from .errors import ImageFormatError
from .image import load_machine
from .machine import Machine
from .migration import fieldize

# 包根目录（recenv 的父目录），通过 PYTHONPATH 传给全新进程，使其无需安装
# 即可 ``python -m recenv._worker``；若 recenv 已安装则该前缀无害。
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def initial_machine(program: "ast.Expr | ast.Program", step_limit: int = 10_000) -> Machine:
    """字段化程序并构造初始执行机。

    入参可以是 :class:`~recenv.ast_nodes.Program` 或源 AST 表达式
    （后者先经 :func:`recenv.convert` 转换）。
    """
    if not isinstance(program, ast.Program):
        program = convert(program)
    return Machine(fieldize(program), step_limit=step_limit)


def save_initial_image(program: ast.Program, path: str,
                       step_limit: int = 10_000) -> Machine:
    m = initial_machine(program, step_limit=step_limit)
    save_machine_image(m, path)
    return m


def save_machine_image(m: Machine, path: str) -> None:
    from .image import dump_machine
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dump_machine(m), fh, ensure_ascii=False, sort_keys=True)


def run_segment(
    checkpoint_path: str,
    step_budget: int,
    *,
    python_exe: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """在全新 Python 进程中恢复检查点并推进 ``step_budget`` 个计步归约。

    成功（paused/finished）时用新镜像覆盖 checkpoint_path，使下一段可以
    接着恢复。返回状态摘要 ``{status, result, steps, events}``；
    出错时检查点文件保持为上一段的镜像不动，返回 error 摘要。
    """
    if step_budget < 0:
        raise ValueError("step_budget must be non-negative")
    exe = python_exe or sys.executable
    # 最小环境：只给解释器定位所需 PATH 与包搜索路径 PYTHONPATH，
    # 不读取/转发账号、密钥或其他环境配置。
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": _PKG_ROOT}
    with tempfile.NamedTemporaryFile(
        prefix="recenv-seg-", suffix=".json", delete=False, mode="w",
        encoding="utf-8",
    ) as tmp:
        out_path = tmp.name
    try:
        proc = subprocess.run(
            [exe, "-m", "recenv._worker", checkpoint_path,
             str(step_budget), out_path],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if proc.returncode != 0:
            raise ImageFormatError(
                f"worker exited {proc.returncode}: {proc.stderr.strip()[:400]}"
            )
        with open(out_path, encoding="utf-8") as fh:
            out = json.load(fh)
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    status = out.get("status")
    if status in ("paused", "finished"):
        # 提交新检查点（先在内存完整解析通过再落盘覆盖）
        image = out["image"]
        load_machine(image)  # 完整校验；失败抛 ImageFormatError，旧检查点保留
        with open(checkpoint_path, "w", encoding="utf-8") as fh:
            json.dump(image, fh, ensure_ascii=False, sort_keys=True)
        return {
            "status": status,
            "result": out.get("result"),
            "steps": out.get("steps"),
            "events": [tuple(ev) for ev in out.get("events", [])],
        }
    return out  # error 摘要

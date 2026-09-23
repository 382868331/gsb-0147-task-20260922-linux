"""跨进程恢复 worker 入口（``python -m recenv._worker``）。

用法::

    python -m recenv._worker <输入镜像> <本次步数预算> <输出镜像>

从输入镜像完整校验并恢复执行机，推进至多 <步数预算> 个计步归约，
然后把机器状态（paused / finished / error）连同结果、事件、
``fid_trace`` 一并写入输出 JSON。不与父进程共享任何 Python 对象——
身份只能来自镜像里的稳定 OID。
"""

from __future__ import annotations

import json
import sys

from .errors import LangRuntimeError, StepLimitError
from .image import load_image


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python -m recenv._worker <in_image> <step_budget> <out_image>",
              file=sys.stderr)
        return 2
    in_path, budget_s, out_path = argv
    try:
        budget = int(budget_s)
        if budget < 0:
            raise ValueError
    except ValueError:
        print("step_budget must be a non-negative integer", file=sys.stderr)
        return 2

    m = load_image(in_path)
    out: dict
    try:
        if m.state == "finished":
            ran = 0
        else:
            ran = m.run(step_budget=budget)
    except (StepLimitError, LangRuntimeError) as exc:
        out = {
            "status": "error",
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "fid_trace": list(getattr(exc, "fid_trace", [])),
                "ir_node": getattr(exc, "ir_node", None),
            },
            "steps": m.steps,
            "events": [list(ev) for ev in m.events],
        }
    else:
        from .image import dump_machine
        out = {"status": m.state, "result": m.result, "steps": m.steps,
               "ran": ran,
               "events": [list(ev) for ev in m.events],
               "image": dump_machine(m)}

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

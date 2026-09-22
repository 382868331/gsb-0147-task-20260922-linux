"""固定输入演示：环境布局转换的正常结果 + 一个实际触发的错误边界。

运行：``python demo.py``（仅需 Python 3.14 标准库，数秒内完成）。

演示内容：
1. 正常程序：含被捕获的可变变量（共享 cell）、互递归 letrec 组
   （共享组帧）、按绑定 ID 排序的自由变量；打印转换后的环境布局，
   并由独立源解释器与目标解释器分别计算、对照返回值与副作用序列。
2. 边界：引用未绑定变量的程序在转换阶段被静态拒绝（可定位错误）；
   另演示执行步数预算耗尽（StepLimitError）与“程序错误”明确区分。
"""

from __future__ import annotations

from closure_layout import parse, convert, run, run_source
from closure_layout.errors import (
    StepLimitError,
    UnboundVariableError,
)
from closure_layout.layout import render

NORMAL_PROGRAM = """
(let (counter 0)
  (let (bump (lambda () (set counter (+ counter 1))))
    (letrec
      ((even? (lambda (n) (if0 n 1 (odd? (+ n -1)))))
       (odd?  (lambda (n) (if0 n 0 (even? (+ n -1))))))
      (+ (+ (bump) (bump))        ; 副作用：counter -> 1 -> 2，返回 1、2
         (even? 10)))))           ; 10 是偶数 -> 1；合计 (1+2)+1 = 4
""".strip()

UNBOUND_PROGRAM = "(+ 1 missing-name)"

DIVERGING_PROGRAM = "(letrec ((loop (lambda () (loop)))) (loop))"


def main() -> None:
    print("########## 1) 正常结果：环境布局 ##########")
    print("源程序:")
    print(NORMAL_PROGRAM)
    print()

    tree = parse(NORMAL_PROGRAM)
    program = convert(tree)

    print(render(program))
    print()

    source_result = run_source(tree)
    target_result = run(program)

    print("执行结果（独立源解释器 / 转换后目标解释器）:")
    print(f"  返回值      : {source_result.value} / {target_result.value}")
    print(f"  副作用序列  : {source_result.trace} / {target_result.trace}")
    print(f"  目标执行步数: {target_result.steps}（预算 10000）")
    assert source_result.value == target_result.value == 4
    assert source_result.trace == target_result.trace == [(0, 1), (0, 2)]
    print("  -> 两侧返回值与副作用序列完全一致。")
    print()

    print("########## 2a) 实际触发的拒绝：未绑定变量 ##########")
    print(f"源程序: {UNBOUND_PROGRAM}")
    try:
        convert(parse(UNBOUND_PROGRAM))
    except UnboundVariableError as e:
        print(f"转换被拒绝（{type(e).__name__}）: {e}")
        print("  该检查在执行前进行，分支是否执行不影响判定。")
    print()

    print("########## 2b) 实际触发的边界：步数预算耗尽 ##########")
    print(f"源程序: {DIVERGING_PROGRAM}")
    for label, execute in (
        ("源解释器", lambda: run_source(parse(DIVERGING_PROGRAM))),
        ("目标解释器", lambda: run(convert(parse(DIVERGING_PROGRAM)))),
    ):
        try:
            execute()
        except StepLimitError as e:
            print(f"{label} 抛出 {type(e).__name__}: {e.message}")
    print("  -> 预算耗尽（StepLimitError）与未绑定等已确定程序错误分开报告。")


if __name__ == "__main__":
    main()

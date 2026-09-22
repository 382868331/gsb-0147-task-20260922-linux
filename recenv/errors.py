"""错误类型与错误语义。

所有可定位错误的消息都以 ``"<loc>: "`` 开头；``loc`` 来自 AST 节点构造时
传入的位置标签（任意字符串，通常是 "line:col"）。
"""

from __future__ import annotations


class RecenvError(Exception):
    """本库所有错误的基类。"""


class ASTError(RecenvError):
    """源 AST 形状/类型非法（构造或转换前校验阶段）。"""


class NodeLimitError(RecenvError):
    """源 AST 节点数超过 500。

    Attributes:
        count: 实际节点数
        limit: 允许的上限（500）
    """

    def __init__(self, count: int, limit: int, loc: str | None = None) -> None:
        self.count = count
        self.limit = limit
        prefix = f"{loc}: " if loc else ""
        super().__init__(
            f"{prefix}AST node count {count} exceeds limit {limit}"
        )


class ScopeError(RecenvError):
    """静态作用域/绑定错误（重名、对 letrec 名赋值等）。"""


class UnboundVariableError(ScopeError):
    """变量未绑定。"""

    def __init__(self, name: str, loc: str | None = None) -> None:
        self.name = name
        prefix = f"{loc}: " if loc else ""
        super().__init__(f"{prefix}unbound variable: {name!r}")


class StepLimitError(RecenvError):
    """执行步数预算耗尽（与“无解”是不同概念；本语言没有可满足性搜索）。

    Attributes:
        steps: 耗尽时已执行的步数
        limit: 步数预算（默认 10000）
        events: 耗尽前已经产生的副作用事件（可能为空）
    """

    def __init__(
        self,
        steps: int,
        limit: int,
        loc: str | None = None,
        events: list | None = None,
    ) -> None:
        self.steps = steps
        self.limit = limit
        self.events = events or []
        prefix = f"{loc}: " if loc else ""
        super().__init__(
            f"{prefix}step budget exhausted: {steps} steps (limit {limit})"
        )


class LangRuntimeError(RecenvError):
    """语言运行期错误：对非整数加法、调用非函数、参数数量不符等。"""

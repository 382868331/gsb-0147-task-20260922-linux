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


class SnapshotError(RecenvError):
    """快照引用图 / 代码版本校验失败（恢复在发布新环境前完整拒绝）。

    恢复是全有或全无的：校验阶段抛出本错误时，不会产生任何部分恢复的机器。
    """


class CodeVersionError(RecenvError):
    """函数版本表问题：未注册版本、代码哈希不符、版本间元数/成员不一致等。"""


class MigrationError(RecenvError):
    """显式字段迁移规则非法或迁移无法满足：

    * 重命名指向不存在的字段 / 与保留字段冲突；
    * 删除的字段仍被新版本引用；
    * 新字段没有常量整数默认值，或默认值类型非法；
    * 新旧字段集在应用规则后仍不一致；
    * 互递归组缺少成员等。
    """


class HeapLimitError(RecenvError):
    """堆对象数超过显式预算（与“无解”无关，纯属资源边界）。"""

    def __init__(self, count: int, limit: int) -> None:
        self.count = count
        self.limit = limit
        super().__init__(f"heap object count {count} exceeds limit {limit}")


class FrameLimitError(RecenvError):
    """显式调用帧数超过预算。"""

    def __init__(self, depth: int, limit: int) -> None:
        self.depth = depth
        self.limit = limit
        super().__init__(f"call frame depth {depth} exceeds limit {limit}")

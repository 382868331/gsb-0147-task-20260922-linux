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
    """语言运行期错误：对非整数加法、调用非函数、参数数量不符等。

    栈式执行机（:mod:`recenv.machine`）抛出时会在异常实例上附带
    ``fid_trace``（``[(fid, code_version), ...]``，调用栈底在前）与
    ``ir_node``（出错时正在归约的 IR 节点类型名），用于校验
    “连续执行 / 分段恢复”的异常位置一致；旧解释器不设置这两个属性。
    """


# ---------------------------------------------------------------------------
# 持久化 / 版本迁移相关错误（新增能力，独立格式 recenv-image/1）
# ---------------------------------------------------------------------------


class ImageFormatError(RecenvError):
    """镜像格式非法：版本不识别、结构缺字段、引用悬空、对象类型不符等。

    加载器对镜像做完整的引用图校验，不信任镜像内任何自述合法性标志。
    """


class MachineStateError(RecenvError):
    """执行机状态不允许所请求的操作（如在非安全点保存/升级、终态后继续）。"""


class UpgradeValidationError(RecenvError):
    """升级包被静态拒绝：代码版本不匹配，或字段迁移规则不合法。

    合法字段迁移仅限：重命名字段、删除“新版本代码不再引用”的字段、
    增加带*常量*默认值的字段。验证器自行重算字段流与引用集合，
    不读取升级包内任何布尔标志。
    """


class UpgradeAborted(RecenvError):
    """升级在提交前被中止（含测试注入的失败）；暂存区已丢弃，无部分发布。

    Attributes:
        reason: 中止原因（注入点名称或内部错误说明）
        rolled_back: 恒为 True；调用方可据此核对（但检查器应自行复查
            堆与版本表确实未变，而不是信任此标志）
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.rolled_back = True
        super().__init__(f"upgrade aborted before commit: {reason}")

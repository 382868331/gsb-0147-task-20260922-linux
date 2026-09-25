# recenv — 递归闭包的环境布局转换 + 真尾位置跳转

为离线编译器中间表示工程提供的可复用模块：把一个带词法作用域、闭包、
可变变量和同时绑定递归函数组的小型源语言，**闭包转换**为无自由变量的
函数表 + 显式环境构造 + 主表达式，并配套两个独立解释器用于对照验收。
转换只在**真正的尾位置**生成尾调用；目标解释器用**显式调用帧**管理
非尾调用、尾调用**替换当前帧**，执行不借助 Python 调用栈。

- Python 版本：**3.14.7**（仅标准库，`dataclasses` / `random` / `unittest` / `sys`）
- 依赖：**无第三方依赖**，安装 Python 后即可运行
- 网络 / 账号 / 密钥 / 数据库 / 中间件 / 云 API / Docker：均不需要

## 快速命令

```bash
# 必要测试（在仓库根目录）
./.venv/bin/python -m unittest discover -s tests -v

# 固定输入演示（实测约 1.7 秒，上限 90 秒；真实计算，无 sleep、无预录结果）
./.venv/bin/python demo.py
```

两条命令均完全离线。

## 包结构

```
recenv/
  ast_nodes.py     源 AST 节点 + 目标 IR 数据结构（frozen dataclass）
  errors.py        错误类型与错误语义
  resolver.py      静态解析：绑定 ID、词法遮蔽、可变性分析、自由变量收集
  convert.py       核心：convert(AST) -> Program（闭包转换 + 显式环境 + 尾位置标记）
  source_interp.py 独立源解释器（递归下降参考实现，保留；不导入 convert）
  target_interp.py 目标 IR 解释器（显式调用帧 + 续体栈的迭代求值机）
tests/
  proggen.py             确定性程序生成器（固定随机种子）
  test_ast_validation.py 输入校验与可定位错误
  test_semantics.py      手工语义用例（闭包共享/互递归/遮蔽/未绑定等）
  test_differential.py   源 vs 目标 差分测试（期望值来自独立源解释器，含精确步数）
  test_structures.py     深度/遮蔽/共享 cell 等结构性边界
  test_limits.py         500 节点、10000 步、失败不留部分状态
  test_tailcalls.py      尾位置判定、帧数有界性、非尾帧数增长、参数/异常对照
demo.py                  固定输入演示
```

## 公开接口

```python
from recenv import (
    Int, Var, Add, Let, Lambda, Call, If0, Set, RecBinding, LetRec,  # 源 AST
    convert,             # convert(Expr) -> Program
    eval_source,         # (Expr, step_limit=10000) -> (int, events)
    eval_target,         # (Program, step_limit=10000) -> (int, events)
    eval_target_stats,   # (Program, step_limit=10000) -> RunStats
    TCall, TTailCall,    # 目标 IR：非尾调用 / 真尾调用
    RunStats,            # result/events/steps/max_active_frames/tail_jumps
    MAX_NODES,                 # 500
    DEFAULT_STEP_LIMIT,        # 10000
    errors,           # ASTError / UnboundVariableError / ScopeError /
                      # NodeLimitError / StepLimitError / LangRuntimeError
)
```

### 源语言

整数、`Add`、变量、`Let`、`Lambda`、`Call`、`If0`、`Set`、同时绑定的
`LetRec` 函数组。求值顺序严格从左到右：`Call` 先求函数再依次求实参，
`Add` 先左后右，`Let` 先求值再入作用域，`If0` 只求被选中的分支。

程序结果必须是整数；函数是一等值但不能与整数混用。

### 目标 Program

- `functions: dict[fid, TFunction]`：函数体**无自由变量**，外部引用全部
  变成对环境槽 `(depth, slot)` 的显式访问；
- `main: TExpr`：主表达式，在空环境链上求值；
- `layout: dict[fid, list[str]]`：每个函数激活帧逐槽的语义说明
  （`val:` / `cell:` / `cell-rec:`，含名字与绑定 ID），用于展示环境布局；
- `n_nodes`：源 AST 节点数。

调用节点两种：`TCall`（非尾位置，建立新帧）与 `TTailCall`（真尾位置，
替换当前帧），二者均带源节点 `loc` 以便运行期错误定位。

## 尾位置规则（核心约定）

转换只把以下位置当作尾位置（生成 `TTailCall`）：

1. **函数体的结果位置**（lambda 体顶层调用）；
2. **`if0` 被选中的结果分支**（`then` / `else`；条件不是尾位置）；
3. **`let` 的 body 与 `letrec` 的组体 body**（绑定值不是尾位置）；
4. 以上位置经 `let`/`letrec`/`if0` 透传，例如
   `let x = ... in if0 c then f() else g()` 中 `f()`/`g()` 是尾调用。

明确**不是**尾位置（保持 `TCall`）：`Add` 的两个操作数、`Set` 的右侧、
`Let` 的绑定值、`Call` 的函数位置与每个参数位置、闭包环境槽抽取。
进入 `Lambda` 开始一条新的尾位置链，与外层是否尾位置无关。

## 目标执行器：显式调用帧

目标解释器是迭代 CEK 机，**语言调用不占用 Python 调用栈**（执行期间
Python 栈深度恒为小常数，测试中有直接测量）：

- 维护显式续体栈；栈中的帧边界标记数量即**当前活动函数调用帧数**，
  其峰值记录为 `RunStats.max_active_frames`。
- `TCall`：函数与参数严格从左到右全部求值成功、callee/元数校验通过后，
  压入新帧边界（帧数 +1），跳转函数体；返回时边界弹出（帧数 -1）。
- `TTailCall`：同样在**全部参数求值成功且校验通过**后，丢弃当前帧边界
  及其上的残留续体，原地替换为新帧边界并跳转，**帧数不增加**
  （计入 `RunStats.tail_jumps`）。参数求值出错或元数/callee 校验失败时
  当前帧不会被提前替换——异常直接传播，调用者帧完好。
- 主表达式尾位置进入的第一个函数帧用根帧边界标记，返回值即程序结果。
- 词法环境仍是闭包转换后的显式记录链：共享 `Cell` 与 letrec 组环境的
  运行期语义不变；`TWithFrame` 追加的记录随续体中的链快照释放。

因此：交替尾递归 even/odd 跑 200_000 层（宿主递归深度上限恢复为通常的
1000）时 `max_active_frames == 1`；把递归调用放在 `Add` 操作数位置的
非尾递归 sumto(n) 时 `max_active_frames == n + 1`。两种结果都在 demo
与 `tests/test_tailcalls.py` 中现场验证。

## 环境布局设计（核心取舍）

1. **绑定 ID**：解析器在进入作用域时按声明顺序分配不可变整数 ID；
   letrec 组内连续分配。自由变量**按绑定 ID 排序**入捕获槽，因此同名
   遮蔽的两个绑定（ID 不同）绝不会按名字字符串混淆。
2. **帧链**：运行期环境是记录链；函数激活时建立一帧，槽位依次为
   “按 ID 排序的捕获槽” + “按声明顺序的参数槽”。激活帧把自由变量
   扁平化到 depth 0，故函数体内不存在自由变量。
3. **共享 cell**：被 `set` 的 `let` 绑定与被 `set` 的参数在槽内放
   `Cell`；闭包抽取环境时 Cell 原样传递，多个闭包因此共享同一个可变
   单元。未被 `set` 的绑定放原值（`val:`），不引入间接。
4. **letrec 组环境**：组帧每槽一个“未填充 Cell”，先在组作用域中构造
   全部成员闭包（共享同一组帧，从而互递归），再一次性填充；重复填充或
   填充前读取是运行期错误。对 letrec 名做 `set` 是静态错误
   （函数槽不可重新赋值）。
5. **显式簿记**：`TWithFrame`（追加帧）、`TBegin`（填充后执行组体）、
   `TMkCell` / `TFillCell` 只表达布局，不计入执行步数。

## 输入上限与错误语义

| 边界 | 值 | 超限行为 |
| --- | --- | --- |
| 源 AST 节点数 | **500**（含所有表达式节点与 RecBinding） | `NodeLimitError(count, limit)` |
| 执行步数 | **10000**（源/目标解释器同一口径，可显式调大用于长递归演示） | `StepLimitError(steps, limit, events=...)` |
| 调用帧数 | **无独立上限**；受步数预算约束，峰值由 `max_active_frames` 给出 | —— |
| 整数字面量 | 必须 `int`；`bool` 拒绝；float 仅接受有限整值，拒绝 NaN/Infinity | `ASTError` |
| 名字 | 非空字符串；lambda 参数/letrec 组内不得重名 | `ASTError` |
| 未绑定变量 / 对 letrec 名 set | 静态拒绝 | `UnboundVariableError` / `ScopeError` |
| 调用非函数、元数不符、非整数参与加法/if0/set | 运行期拒绝 | `LangRuntimeError` |

- 所有可定位错误消息以 `"<loc>: "` 开头，`loc` 来自节点构造参数
  （如 `Var("x", loc="行3:列8")`）；IR 的调用/加法/条件/赋值节点携带
  源 `loc`，因此目标解释器与源解释器的运行期错误位置一致（闭包值的
  内部类型名除外）。
- `convert` 是纯函数，失败只通过异常传播，调用方拿不到任何部分
  `Program`；解释器每次调用新建状态，失败不影响后续调用。
- **预算耗尽 ≠ 证明无解**：本语言没有可满足性搜索；`StepLimitError`
  专指执行步数预算用完，与静态确定的错误（未绑定、类型错误）严格区分。
  `StepLimitError.events` 保留耗尽前已产生的副作用序列。
- **步数口径**：一个“步”对应一次源语言归约（字面量/变量读取/加法/
  调用/条件/闭包构造/赋值）。源 `Call` 无论被翻译成 `TCall` 还是
  `TTailCall` 都计 1 步；环境布局簿记（`TMkCell`/`TFillCell`/
  `TWithFrame` 的簿记部分/`TBegin`）不计步，内嵌表达式照常计。
  差分测试对 180+ 个生成程序逐例断言两解释器**精确步数相等**。

## 数值与误差口径

本语言只有**任意精度整数**（Python `int`）与整数加法，**不存在浮点
运算结果**，因此没有浮点误差区间；`float` 仅在构造 `Int` 时按
“必须是有限整值”接受或拒绝（拒绝 `NaN`/`±Infinity`/非整值），一经
接受即转为 `int`，后续运算精确。

## 验收方式

- **独立对照**：期望值由 `source_interp`（名字作用域 + 帧指针闭包的
  独立递归下降实现，不导入 `convert`）给出；`target_interp` 的返回值、
  副作用事件序列 `("set", 名字, 整数)`、运行期错误位置逐项对照。
- **差分测试**：`tests/proggen.py` 用固定种子生成 180+ 个程序
  （含产生 `set` 副作用、二/三函数互递归组、捕获外层可变 cell 的
  递归函数），全部双解释器对拍并比较精确步数。
- **尾调用验证**：200_000 层交替互递归（宿主深度上限恢复为 1000）
  帧数恒为 1；非尾递归帧数随深度精确增长；执行时 Python 栈深度实测
  为常数；压低宿主递归上限后目标器照常运行，而源解释器如期
  `RecursionError`，证明不是靠调大 Python 限制。
- **参数严格性**：专门用例验证“一个参数 `set` 捕获变量，后续参数与
  函数体读到新值”，以及尾调用参数求值出错时当前帧不被替换
  （出错瞬间 `active_frames()`/`tail_jumps` 保持调用前状态）。
- 闭包共享计数通过运行时 hook 直接断言两个闭包捕获记录中的 `Cell`
  是同一对象；互递归断言组帧中两个 rec-cell 的对象身份；同名遮蔽断言
  内层/外层绑定的布局 ID 不同。

## 设计取舍与已知限制

- 只实现题目契约内的语言子集：整数 + 加法 + 闭包 + if0 + set +
  同时绑定 letrec。**不**宣称与任何行业标准语言/字节码/ABI 兼容；
  没有乘法、字符串、数据结构、异常值、I/O 原语。
- **源解释器刻意保留为递归下降**参考实现（导入时把 Python 递归余量
  上调到 100_000 以保证万步预算先于宿主栈报错）；**目标解释器**才是
  显式调用帧的迭代机，尾调用能力只在转换后的 IR 执行路径上提供。
- 没有独立的调用帧数量上限：终止性由步数预算保证；需要限制嵌套时由
  调用方设置 `step_limit`，或检查 `RunStats.max_active_frames`。
- letrec 只允许 lambda 绑定（递归函数组语义）；非函数的递归绑定不在
  契约内。
- 短演示不穷举上限规模；500/10000 的边界由单元测试精确覆盖
  （500 通过、501 拒绝；无限尾循环帧数恒为 1 并触发步数错误）。

# recenv — 递归闭包的环境布局转换 + 尾位置跳转执行

为离线编译器中间表示工程提供的可复用模块：把一个带词法作用域、闭包、
可变变量和同时绑定递归函数组的小型源语言，**闭包转换**为无自由变量的
函数表 + 显式环境构造 + 主表达式，并配套两个独立解释器用于对照验收。
目标执行器用**显式调用帧 + trampoline** 实现真正的尾位置跳转：尾调用
替换当前调用帧（活动帧数有界，且不依赖宿主 Python 递归深度），非尾调用
才压入新帧。

- Python 版本：**3.14.7**（仅标准库，`dataclasses` / `random` / `unittest` / `sys`）
- 依赖：**无第三方依赖**，安装 Python 后即可运行
- 网络 / 账号 / 密钥 / 数据库 / 中间件 / 云 API / Docker：均不需要

## 快速命令

```bash
# 必要测试（在仓库根目录；使用随仓的解释器）
./.venv/bin/python -m unittest discover -s tests -v

# 固定输入演示（约 0.3 秒，上限 90 秒；真实计算，无 sleep、无预录结果）
./.venv/bin/python demo.py
```

两条命令均完全离线，且使用同一个 Python 3.14.7 解释器。

## 包结构

```
recenv/
  ast_nodes.py     源 AST 节点 + 目标 IR 数据结构（frozen dataclass；含 TTailCall）
  errors.py        错误类型与错误语义
  resolver.py      静态解析：绑定 ID、词法遮蔽、可变性分析、自由变量收集
  convert.py       核心：convert(AST) -> Program（闭包转换 + 显式环境布局 + 尾位置标注）
  source_interp.py 独立源解释器（参考实现；递归下降，不导入 convert）
  target_interp.py 目标 IR 解释器（trampoline + 显式调用帧，执行 convert 的输出）
tests/
  proggen.py             确定性程序生成器（固定随机种子）
  test_ast_validation.py 输入校验与可定位错误
  test_semantics.py      手工语义用例（闭包共享/互递归/遮蔽/未绑定等）
  test_differential.py   源 vs 目标 差分测试（期望值来自独立源解释器，含精确步数）
  test_structures.py     嵌套闭包/嵌套 letrec/捕获 ID 排序等结构边界
  test_limits.py         500 节点、10000 步、失败不留部分状态
  test_tail_calls.py     尾位置判定、帧有界性、非尾帧增长、参数求值序、异常位置对拍
demo.py                  固定输入演示
```

## 公开接口

```python
from recenv import (
    Int, Var, Add, Let, Lambda, Call, If0, Set, RecBinding, LetRec,  # 源 AST
    convert,                    # convert(Expr) -> Program（含尾位置标注）
    eval_source,                # (Expr, step_limit=10000) -> (int, events)
    eval_target,                # (Program, step_limit=10000) -> (int, events)
    run_program_with_stats,     # (Program, step_limit=10000) -> RunStats
    MAX_NODES,                  # 500
    DEFAULT_STEP_LIMIT,         # 10000
    errors,                     # ASTError / UnboundVariableError / ScopeError /
                                # NodeLimitError / StepLimitError / LangRuntimeError
)
from recenv.target_interp import RunStats
# RunStats 字段：value, events, steps, max_call_frames, tail_jumps
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

目标 IR 用两种调用节点区分位置：普通 `TCall`（非尾，执行时压新帧）与
`TTailCall`（尾位置，执行时替换当前帧）；`TPrim` / `TIf0` / `TCellSet` /
两种调用节点都带源节点 `loc`，使运行期异常位置可与源解释器逐项对拍。

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

## 尾位置与尾调用执行（trampoline + 显式调用帧）

### 哪些位置是尾位置

转换器对每个源调用点按下面的规则标注；尾位置的调用生成 `TTailCall`，
其余生成普通 `TCall`：

| 位置 | 是否尾位置 | 生成 |
| --- | --- | --- |
| 函数体的结果位置 | 是 | `TTailCall` |
| `if0` 被选中的**结果分支**（then / else） | 是（继承） | `TTailCall` |
| `let` / `letrec` 的 **body** | 是（继承） | `TTailCall` |
| 嵌套 `if0` 结果分支里的调用 | 是（逐层继承） | `TTailCall` |
| `Add` 的左/右操作数 | **否** | `TCall` |
| `Set` 的右侧表达式 | **否** | `TCall` |
| `Call` 的函数位置与每个实参 | **否** | `TCall` |
| 主表达式 `main` 中的调用（main 不是函数体，没有可替换的调用帧） | **否** | `TCall` |

`let` 绑定值、`if0` 条件、`letrec` 填充表达式同样不是尾位置。
尾位置性只沿"结果通路"（函数体 → if0 分支 → let/letrec body）传播。

### 执行模型

`eval` 沿 IR 语法树递归下降；语法树嵌套深度由 **500 节点上限**界定，
与一个函数被调用多少次无关。

- 遇到 `TTailCall`：先在**当前帧**中严格从左到右求函数值与全部实参，
  全部成功后抛出内部控制信号 `_TailJump`；`_invoke` 的循环接住它，用
  目标函数的激活记录**替换**当前记录后继续——既不增加 Python 栈深度，
  也不增加活动调用帧数。
- 遇到 `TCall`：函数与实参求值后，`_invoke` **压入一个新调用帧**
  （`call_frames += 1`）再执行函数体，返回时弹帧。
- **参数求值出错不会提前替换帧**：`_TailJump` 只在函数值和每个实参都
  求值成功后才发出；任一实参抛错，当前帧原样保留并随 `finally` 正常弹回。

### 调用帧计量（不是藏起来的 Python 递归深度）

`run_program_with_stats` 返回 `RunStats`：

- `max_call_frames`：执行期间**峰值活动调用帧数** = 尚未返回的非尾函数
  激活数（主表达式 main 不计帧）。纯尾递归/互尾递归恒为 1；非尾嵌套
  （如 `1 + s(n-1)`）随深度线性增长，`s(50)` 峰值为 51。
- `tail_jumps`：尾调用帧替换总次数。
- `steps`：源语言归约步数（口径见下）。

演示与 `tests/test_tail_calls.py` 用两种方式证明这不是"把 Python 递归
深度藏起来"：

1. f/g 交替互尾递归跑 **20000 层**（远超 CPython 默认递归深度量级），
   `max_call_frames == 1`；并在运行前把宿主递归限制现场压到 **200**，
   仍正常返回——长尾路径走的是迭代循环。
2. 非尾递归 `s(n)=1+s(n-1)` 的峰值帧随 `n` 增长到 `n+1`，在压低宿主
   限制时确实抛 `RecursionError`——真实的非尾嵌套没有被尾跳转吞掉。

### 严格求值序与共享 cell

尾调用的实参仍在当前帧、按从左到右求值。专门覆盖了"一个实参通过闭包
`set` 修改某个共享捕获 cell、后一个实参通过另一个闭包读取同一个 cell"
的场景：后求值的实参必须读到前一个实参刚写入的值（交换两实参顺序则读到
旧值）。该用例与源解释器对拍返回值、`set` 事件序列与异常位置。

## 输入上限与错误语义

| 边界 | 值 | 超限行为 |
| --- | --- | --- |
| 源 AST 节点数 | **500**（含所有表达式节点与 RecBinding） | `NodeLimitError(count, limit)` |
| 执行步数 | **10000**（源/目标解释器同一口径） | `StepLimitError(steps, limit, events=...)` |
| 整数字面量 | 必须 `int`；`bool` 拒绝；float 仅接受有限整值，拒绝 NaN/Infinity | `ASTError` |
| 名字 | 非空字符串；lambda 参数/letrec 组内不得重名 | `ASTError` |
| 未绑定变量 / 对 letrec 名 set | 静态拒绝 | `UnboundVariableError` / `ScopeError` |
| 调用非函数、元数不符、非整数参与加法 | 运行期拒绝 | `LangRuntimeError` |

- 所有可定位错误消息以 `"<loc>: "` 开头，`loc` 来自节点构造参数
  （如 `Var("x", loc="行3:列8")`）。
- `convert` 是纯函数，失败只通过异常传播，调用方拿不到任何部分
  `Program`；解释器每次调用新建状态，失败不影响后续调用。
- **预算耗尽 ≠ 证明无解**：本语言没有可满足性搜索；`StepLimitError`
  专指执行步数预算用完，与静态确定的错误（未绑定、类型错误）严格区分。
  `StepLimitError.events` 保留耗尽前已产生的副作用序列。
- 步数口径：一个“步”对应一次源语言归约（字面量/变量读取/加法/调用/
  条件/闭包构造/赋值）；环境布局簿记不计步，**尾跳转本身也不额外计步**
  （它只替换帧，源语言层面仍是那一次调用归约）。源与目标解释器在对拍
  用例上不仅结果一致，**精确步数也逐项相等**（见 `test_differential.py`
  与 `test_tail_calls.py`）。

## 数值与误差口径

本语言只有**任意精度整数**（Python `int`）与整数加法，**不存在浮点
运算结果**，因此没有浮点误差区间；`float` 仅在构造 `Int` 时按
“必须是有限整值”接受或拒绝（拒绝 `NaN`/`±Infinity`/非整值），一经
接受即转为 `int`，后续运算精确。

## 验收方式

- **独立对照**：期望值由 `source_interp`（名字作用域 + 帧指针闭包的
  独立实现，不导入 `convert`）给出；`target_interp` 的返回值与副作用
  事件序列 `("set", 名字, 整数)` 逐项对照。
- **差分测试**：`tests/proggen.py` 用固定种子生成 180+ 个程序
  （含产生 `set` 副作用、二/三函数互递归组、捕获外层可变 cell 的
  递归函数），全部双解释器对拍。
- 闭包共享计数通过运行时 hook 直接断言两个闭包捕获记录中的 `Cell`
  是同一对象；互递归断言组帧中两个 rec-cell 的对象身份；同名遮蔽断言
  内层/外层绑定的布局 ID 不同。
- **尾调用对拍**（`test_tail_calls.py`）：结构性断言尾位置只出现在函数体 /
  if0 分支 / let·letrec body；20000 层交替互尾递归 `max_call_frames == 1`
  且宿主递归限制压到 200 仍成功；非尾递归峰值帧随深度增长到 `n+1`；
  参数求值出错时帧不替换、计数回落；跨实参的共享 cell 读写序与异常位置
  字符串源/目标逐项相等。
- 往返一致（源→IR→执行结果一致）是上述对照的一部分，不作为唯一证据。

## 设计取舍与已知限制

- 只实现题目契约内的语言子集：整数 + 加法 + 闭包 + if0 + set +
  同时绑定 letrec。**不**宣称与任何行业标准语言/字节码/ABI 兼容；
  没有乘法、字符串、数据结构、异常值、I/O 原语。
- **尾调用语义是新增能力**：尾位置由转换器静态判定（函数体 / if0 分支 /
  let·letrec body），并非所有调用都优化；`main` 中的调用按非尾处理。
  执行器是在原递归下降 IR 求值器上演进而来的 **trampoline 变体**：
  非尾激活仍真实占用 Python 栈与调用帧（这是必须保留的语义嵌套），
  10000 步默认预算内最深约数千层，故目标执行器仍把 Python 递归限制抬到
  至少 100000，以保证**真实非尾嵌套**先撞步数预算而非宿主深度；长尾路径
  则经 `_TailJump` 迭代、Python 栈深度恒定。把递归限制压到 200 跑
  20000 层交替尾递归仍成功（见 demo / `test_tail_calls.py`），证明尾
  路径并不依赖该抬栈。
- 源解释器 `source_interp` 作为独立参考**原样保留**为递归下降实现，
  未做尾调用处理；深尾递归程序不要用它跑到数千层（会撞宿主深度），
  源/目标对拍一律使用小输入。
- letrec 只允许 lambda 绑定（递归函数组语义）；非函数的递归绑定不在
  契约内。
- 短演示跑 20000 层尾递归约 0.3 秒；500/10000 的边界由单元测试精确覆盖
  （500 通过、501 拒绝；无限递归在两个解释器上都触发步数错误）。

# recenv — 递归闭包的环境布局转换

为离线编译器中间表示工程提供的可复用模块：把一个带词法作用域、闭包、
可变变量和同时绑定递归函数组的小型源语言，**闭包转换**为无自由变量的
函数表 + 显式环境构造 + 主表达式，并配套两个独立解释器用于对照验收。

- Python 版本：**3.14.7**（仅标准库，`dataclasses` / `random` / `unittest` / `sys`）
- 依赖：**无第三方依赖**，安装 Python 后即可运行
- 网络 / 账号 / 密钥 / 数据库 / 中间件 / 云 API / Docker：均不需要

## 快速命令

```bash
# 必要测试（在仓库根目录）
python -m unittest discover -s tests -v

# 本次迭代演示（约 0.1 秒，上限 90 秒；真实计算，无 sleep、无预录结果）
python demo_iteration.py
```

两条命令均完全离线。

如需使用虚拟环境（可选）：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt   # 无第三方依赖；文件仅含说明注释
python -m unittest discover -s tests -v
python demo_iteration.py
```

## 包结构

```
recenv/
  ast_nodes.py     源 AST 节点 + 目标 IR 数据结构（frozen dataclass）
  errors.py        错误类型与错误语义
  resolver.py      静态解析：绑定 ID、词法遮蔽、可变性分析、自由变量收集
  convert.py       核心：convert(AST) -> Program（闭包转换 + 显式环境布局）
  source_interp.py 独立源解释器（参考实现；不导入 convert）
  target_interp.py 目标 IR 解释器（执行 convert 的输出）
tests/
  proggen.py             确定性程序生成器（固定随机种子）
  test_ast_validation.py 输入校验与可定位错误
  test_semantics.py      手工语义用例（闭包共享/互递归/遮蔽/未绑定等）
  test_differential.py   源 vs 目标 差分测试（期望值来自独立源解释器）
  test_limits.py         500 节点、10000 步、失败不留部分状态
  test_partial.py        partial 前缀绑定、过量续应用、零参与严格顺序
demo.py                  既有固定输入演示（保留旧入口）
demo_iteration.py        本次 partial 迭代演示
```

## 公开接口

```python
from recenv import (
    Int, Var, Add, Let, Lambda, Call, Partial, If0, Set,
    RecBinding, LetRec,  # 源 AST
    convert,          # convert(Expr) -> Program
    eval_source,      # (Expr, step_limit=10000) -> (int, events)
    eval_target,      # (Program, step_limit=10000) -> (int, events)
    MAX_NODES,                 # 500
    DEFAULT_STEP_LIMIT,        # 10000
    errors,           # ASTError / UnboundVariableError / ScopeError /
                      # NodeLimitError / StepLimitError / LangRuntimeError
)
```

### 源语言

整数、`Add`、变量、`Let`、`Lambda`、`Call`、`Partial`、`If0`、`Set`、
同时绑定的 `LetRec` 函数组。求值顺序严格从左到右：`Call` / `Partial`
先求函数再依次求实参，`Add` 先左后右，`Let` 先求值再入作用域，`If0`
只求被选中的分支。

`Partial(func, args)` 是独立的前缀应用节点，不改变旧 `Call`：

- `args` 中的值在 partial 发生时立即、依次、只求值一次；
- 已绑定参数少于形参时返回普通闭包，之后可用普通 `Call` 补齐剩余参数；
- 参数刚好收齐时立即进入函数体；但零参 partial 总是先返回一个等价闭包，
  即使目标函数本身是零参函数，也不提前执行函数体；
- 本次提供的参数超过剩余形参时，先收齐并进入函数体，再把多出的实参
  按从左到右应用到返回值；若返回值不是函数则触发运行期错误；
- 旧 `Call` 仍保持精确元数，不会被自动改成柯里化；调用 partial 闭包时
  必须恰好提供“剩余参数个数”。

程序结果必须是整数；函数是一等值但不能与整数混用。

### 目标 Program

- `functions: dict[fid, TFunction]`：函数体**无自由变量**，外部引用全部
  变成对环境槽 `(depth, slot)` 的显式访问；
- `main: TExpr`：主表达式，在空环境链上求值；
- partial 在 IR 中表示为 `TPartial(func, args)`；运行时闭包额外携带
  `prefix` 已绑定值。闭包捕获记录本身不复制，仍共享同一环境槽与递归组帧；
- `layout: dict[fid, list[str]]`：每个函数激活帧逐槽的语义说明
  （`val:` / `cell:` / `cell-rec:`，含名字与绑定 ID），用于展示环境布局；
- `n_nodes`：源 AST 节点数。

## 环境布局设计（核心取舍）

1. **绑定 ID**：解析器在进入作用域时按声明顺序分配不可变整数 ID；
   letrec 组内连续分配。自由变量**按绑定 ID 排序**入捕获槽，因此同名
   遮蔽的两个绑定（ID 不同）绝不会按名字字符串混淆。
2. **帧链**：运行期环境是记录链；函数激活时建立一帧，槽位依次为
   “按 ID 排序的捕获槽” + “按声明顺序的参数槽”。partial 只把已求值的
   参数值保存在闭包 `prefix` 中，不复制捕获记录；真正进入函数体时才追加
   参数槽。激活帧把自由变量扁平化到 depth 0，故函数体内不存在自由变量。
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
| 执行步数 | **10000**（源/目标解释器同一口径） | `StepLimitError(steps, limit, events=...)` |
| 整数字面量 | 必须 `int`；`bool` 拒绝；float 仅接受有限整值，拒绝 NaN/Infinity | `ASTError` |
| 名字 | 非空字符串；lambda 参数/letrec 组内不得重名 | `ASTError` |
| 未绑定变量 / 对 letrec 名 set | 静态拒绝 | `UnboundVariableError` / `ScopeError` |
| 调用/partial 非函数、旧 Call 元数不符、过量应用到非函数、非整数参与加法 | 运行期拒绝 | `LangRuntimeError` |

旧 `Call` 的元数错误保持为“声明形参数 vs 本次实参数”；对 partial 返回的
闭包使用旧 `Call` 时，错误中的期望数量是剩余参数个数。`Partial` 不因为
参数少而报错，只有参数过量且返回值不能继续应用、或函数位置不是函数时才
报错。

- 所有可定位错误消息以 `"<loc>: "` 开头，`loc` 来自节点构造参数
  （如 `Var("x", loc="行3:列8")`）。
- `convert` 是纯函数，失败只通过异常传播，调用方拿不到任何部分
  `Program`；解释器每次调用新建状态，失败不影响后续调用。
- **预算耗尽 ≠ 证明无解**：本语言没有可满足性搜索；`StepLimitError`
  专指执行步数预算用完，与静态确定的错误（未绑定、类型错误）严格区分。
  `StepLimitError.events` 保留耗尽前已产生的副作用序列。
- 步数口径：一个“步”对应一次源语言归约（字面量/变量读取/加法/调用/
  条件/闭包构造/赋值）；环境布局簿记不计步。源与目标解释器在对拍用例
  上结果一致。

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
- `tests/test_partial.py` 对 partial 的返回值、副作用事件和精确步数做
  源/目标逐项对照，覆盖带计数副作用实参、闭包复用、互递归和返回非函数时
  的过量应用检查顺序。
- 闭包共享计数通过运行时 hook 直接断言两个闭包捕获记录中的 `Cell`
  是同一对象；互递归断言组帧中两个 rec-cell 的对象身份；同名遮蔽断言
  内层/外层绑定的布局 ID 不同。
- 往返一致（源→IR→执行结果一致）是上述对照的一部分，不作为唯一证据。

## 设计取舍与已知限制

- 只实现题目契约内的语言子集：整数 + 加法 + 闭包 + if0 + set +
  同时绑定 letrec + 前缀 partial。**不**宣称与任何行业标准语言/
  字节码/ABI 兼容；
  没有乘法、字符串、数据结构、异常值、I/O 原语。
- 解释器为直接的递归下降求值器（非寄存器机/栈机），`sys.setrecursionlimit`
  已留出余量以保证 10000 步预算先于 Python 递归深度报错；这不是
  生产级代码生成器，目标是 IR 布局的正确性与可观察对照。
- letrec 只允许 lambda 绑定（递归函数组语义）；非函数的递归绑定不在
  契约内。
- 短演示不穷举上限规模；500/10000 的边界由单元测试精确覆盖
  （500 通过、501 拒绝；无限递归在两个解释器上都触发步数错误）。

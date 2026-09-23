# recenv — 递归闭包的环境布局、可暂停执行与版本化迁移

为离线编译器中间表示工程提供的可复用模块。在原有的**闭包转换 + 显式环境布局**
之上，本次新增三组能力：

1. **可暂停、可保存、可在新进程恢复的解释器**：显式栈机器 + 堆上的稳定对象 ID，
   序列化保留共享、别名、环与递归函数组；
2. **函数版本表 + 显式字段迁移**：只允许重命名 / 删除未被引用字段 / 增加带
   常量默认值字段；恢复/升级前完整验证引用图与代码版本；
3. **显式安全点 + 步数预算**：在调用栈中间保存续执行状态；升级只在安全点发生，
   旧栈帧继续引用其代码版本，新调用使用新版本，环境引用不混淆。

- Python 版本：**3.14.7**（仅标准库：`dataclasses` / `hashlib` / `json` /
  `random` / `subprocess` / `unittest` / `sys`）
- 依赖：**无第三方依赖**，安装 Python 后即可运行
- 网络 / 账号 / 密钥 / 数据库 / 中间件 / 云 API / Docker：均不需要

## 运行命令

```bash
# 快速回归（原有 60 个测试，秒级）
python -m unittest discover -s tests -v

# 扩展验证（新增 39 个测试：暂停/恢复/快照、版本表、迁移、置换、回滚、规模）
python -m unittest discover -s tests_ext -v

# 固定输入演示（实测约 0.2 秒，硬上限 90 秒；真实计算，无 sleep、无预录结果）
python demo.py
```

两条测试入口分开：`tests/` 是每次改动都应跑的快速回归，`tests_ext/` 是更大的
扩展验证（仍为秒级，约 2–3 秒）。`demo.py` 会派生一个**真实的全新 Python
子进程**，仅从快照字节恢复执行，证明跨进程恢复不依赖进程内对象身份。

## 包结构

```
recenv/
  ast_nodes.py     源 AST + 目标 IR（frozen dataclass）；Lambda/LetRec 新增可选 ver=
  errors.py        错误类型与错误语义
  resolver.py      静态解析：绑定 ID、遮蔽、可变性、自由变量、版本标签/组归属
  convert.py       convert(AST) -> Program（闭包转换 + 显式环境布局）
  source_interp.py 独立源解释器（参考实现；不导入 convert / machine）
  target_interp.py 目标 IR 递归解释器（与以往行为一致）
  codebundle.py    函数版本表：代码登记、规范化、SHA-256 内容哈希、跨版本校验
  heap.py          显式堆：CellObj / ClosureObj / RecordObj + 稳定对象 ID（Ref）
  machine.py       显式栈机器：续体栈、安全点、步数预算、暂停/恢复/旁路/升级
  snapshot.py      快照 JSON 序列化/恢复：引用图与代码哈希完整验证
  migrate.py       字段迁移规则 + 安全点热升级（影子构造→验证→原子提交→可回滚）
tests/             原有快速回归（未删、未放松断言）
tests_ext/         新增扩展验证
demo.py            固定输入演示（含真实子进程恢复）
```

## 公开接口

```python
from recenv import (
    # 原有
    Int, Var, Add, Let, Lambda, Call, If0, Set, RecBinding, LetRec,
    convert, eval_source, eval_target, Program,
    MAX_NODES, DEFAULT_STEP_LIMIT, errors,
    # 新增
    Machine,                 # 显式栈机器
    MigrationRules, FAILPOINTS,
    CodeBundle, build_bundle,
    DEFAULT_FRAME_LIMIT, MAX_HEAP_OBJECTS, snapshot,
)
from recenv.heap import Heap, Ref, CellObj, ClosureObj, RecordObj
```

### 版本标签（新增，可选、关键字专用）

* `Lambda(params, body, ver="name@v")`：独立版本化函数；
* `LetRec(bindings, body, ver="group@v")`：互递归组作为一个版本化单元，
  组整体升级；带标签组的成员 lambda 不再允许自带标签。

旧式不传 `ver=` 的调用一切照旧；标签是纯增量字段，不改变任何位置参数构造。

## 显式栈机器与暂停/恢复

```python
m = Machine(convert(ast), step_limit=10_000,
            frame_limit=100_000, heap_limit=100_000)

m.run_to_completion()                 # 连续执行：(int, events)，语义同 eval_target
status = m.run_segment(budget)        # 执行至多 budget 步，在调用入口安全点暂停
                                      # 返回 "paused" 或 "done"
blob   = snapshot.dumps(m)            # 仅在 paused 安全点可序列化 -> JSON 文本
m2     = snapshot.loads(blob)         # 任意新进程：完整验证后恢复
m2.run_to_completion()                # 继续跑完
```

* **安全点 = 调用入口**。暂停只发生在“函数与实参已全部求值、尚未进入被调方”
  的事务态（内部 `KEnterCall`）。因此无论怎样切分 `run_segment` 预算、暂停
  多少次、是否跨进程，最终返回值、副作用事件序列、异常类型与异常位置都与连续
  执行一致（`tests_ext/test_machine_equiv.py` 用数百个随机预算切分对拍）。
* 运行态是一条 `while` 循环 + 显式续体帧，**值沿栈回送也是迭代的**，不使用
  Python 调用栈保存运行态。可在 `sys.setrecursionlimit(200)` 下跑通 600 层
  显式调用帧（`tests_ext/test_scale.py`）。
* 堆上所有共享状态（cell / 闭包 / 环境记录）由单调 OID 标识；栈帧只持 `Ref`
  或内联整数。

## 对象身份、共享、环与快照格式

快照格式标记为 `"recenv-snapshot-v1"` 的 JSON，包含：版本、预算、步数、事件、
续体栈、控制位、代码表（含每函数 SHA-256）、以 OID 为键的堆对象表。

* **别名不复制**：多个槽指向同一 cell 时，它们存的是同一 OID；
* **环可表达**：letrec 组的成员闭包经组帧 rec-cell 互相指向（闭包→记录→
  rec-cell→伙伴闭包），序列化按图原样恢复；
* **顺序无关**：堆以 OID 为键、恢复两遍装载，随机打乱 JSON 堆键顺序不影响
  引用图与结果（`tests_ext/test_permutation.py`）。

恢复时**先完整验证再发布机器**，任何不一致都在构造机器前抛出 `SnapshotError`：

1. 快照格式/版本标记；
2. 代码：重算每个函数体的 SHA-256 与快照内哈希逐一核对——**检查器不信任快照
   自带的“正确”标志**，篡改哈希或函数体都被拒绝；
3. 续体表达式按内容哈希挂回代码表；找不到即代码版本不符；
4. 从全部根（控制环境、各续体帧、中间值）做引用图可达遍历，悬空 OID 拒绝；
5. 逐对象语义校验：闭包 fkey 已登记、捕获槽数与代码一致、版本元数据一致；
6. 续体帧结构自洽（phase/idx、KEnterCall 元数、在途帧版本钉住、唯一底帧）。

## 函数版本表与字段迁移

`CodeBundle` 是不可变代码登记表，可合并多个 `Program` 容纳同一单元的多个版本。
代码身份是规范化表达式树的 SHA-256；跨版本后继关系强制：

* 版本号必须**恰好 +1**（`bump@1` 不能直接升 `bump@3`）；
* 互递归组的**成员集合不变**（成员名一致）；
* 每成员**元数与参数可变性不变**。

升级用新版本 `Program` + 显式 `MigrationRules`：

```python
MigrationRules(
    rename={"c": "kount"},   # 重命名字段（cell 种类必须保持）
    delete=("unused",),      # 只能删除新版本不再引用的字段
    add={"bias": 100},       # 新字段必须带常量 int 默认值（bool/float 拒绝）
)
```

规则按**单元**给出、按**成员拥有的字段子集**适用：某成员没捕获的字段对它不
生效，但每个成员的新旧字段集都必须被规则恰好闭合，不多不少。新增字段固定为
不可变 `val`；cell 字段只能重命名或删除，不能改种类。

### 升级流程（全有或全无）

`request_upgrade(unit, new_program, rules, failpoint=...)` 登记请求，在**下一个
安全点**原子应用；也可在暂停的安全点用 `apply_pending_upgrade()` 立即应用：

1. 影子代码表登记新程序（`fkey` 加唯一前缀，新旧代码可共存）并校验后继关系；
2. 静态计算并验证字段映射；
3. 扫描堆找出该单元旧版本的全部存活实例（组实例沿 rec-cell 闭包归组）；
4. 在影子空间构造新记录 / 新闭包 / 新 rec-cell（旧对象不动）；
5. 影子图一致性 + 别名保持校验；
6. 提交：并入新代码、装载新对象，再改线引用；任一步抛错都**逆序完整回滚**。

### 旧栈帧与新调用不混淆

* **成员回边**（成员捕获记录、在途成员激活帧中的 rec 槽）保持指向旧组
  rec-cell——所以在途旧版本帧继续自洽地跑旧代码、调旧版本伙伴；
* **外部持有者**（如驱动函数对版本化闭包的捕获）改指向新版本：直接持有的
  换闭包 OID，经 cell 间接持有的换成一个新的“外部 cell”（多个持有者仍共享
  同一个新 cell，别名不复制）；
* 本次正在进入的调用使用调用方已经求出的 callee，因此“旧栈帧旧版本、版本
  边界之后的新调用新版本”由构造保证，不靠标志位。

`FAILPOINTS = ("before_register", "after_shadow_build", "before_commit_code",
"before_commit_objects", "before_rewire", "after_rewire")` 可注入失败；
扩展测试逐点断言：失败后机器的规范快照与升级前**逐位一致**，仍可继续，且最终
结果与从未升级相同。

### 旁路调用

暂停的旧栈之上可旁路运行迁移后的新闭包：

```python
handles = m.apply_pending_upgrade()       # [{成员名: 新闭包 Ref}, ...]
m.invoke(handles[0]["$self"], [1], side=True)
value, events = m.run_side()              # 跑完旁路调用，机器自动回到 paused
m.run_to_completion()                     # 旧栈随后继续
```

独立版本函数的句柄键为 `"$self"`，组成员用成员名（如 `"ev"`/`"od"`）。

## 输入上限、预算与错误语义

| 边界 | 缺省/要求 | 超限行为 |
| --- | --- | --- |
| 源 AST 节点数 | **500**（含所有表达式节点与 RecBinding） | `NodeLimitError(count, limit)` |
| 执行步数 | **10000**（机器/源/目标同一口径） | `StepLimitError(steps, limit, events=...)` |
| 显式调用帧 | 缺省 `frame_limit=100000`（支持 ≥500 层） | `FrameLimitError(depth, limit)` |
| 堆对象 | 缺省 `heap_limit=100000`（支持 ≥1000 对象/200 版本） | `HeapLimitError(count, limit)` |
| 版本标签 | 形如 `name@正整数`，无空白 | 构造期 `ASTError` |
| 整数字面量 | 必须 `int`；`bool` 拒绝；float 仅接受有限整值 | `ASTError` |
| 非法迁移 | 规则不闭合 / 默认值非常量 / 删除仍被引用 / 版本跳跃 / 成员或元数变化 | `MigrationError` / `CodeVersionError` |
| 快照损坏 | 悬空引用、哈希不符、未知函数、帧不一致 | `SnapshotError` |
| 调用非函数、元数不符、非整数加法 | 运行期拒绝 | `LangRuntimeError` |

* 预算耗尽（`StepLimitError`）是**资源边界**，与静态确定的失败（未绑定、类型
  错误、非法迁移）严格区分；异常携带耗尽前的事件序列。本语言没有可满足性
  搜索，不存在 unknown/不可辨识返回值——小规模参考域的例子都给确定答案。
* 步数口径与原目标解释器一致：每个源语言归约一步，环境布局簿记不计步。
  180+ 差分程序上，机器的结果、事件、步数与源/目标解释器逐项一致。
* 所有缺省选择（帧/堆预算、OID 从 1 起、新字段为 val、字段按绑定 ID 排序等）
  都固定在代码中；同一输入重复运行得到同一结果。

## 与原行为的兼容性（逐条）

本次是**增量**改造，原有公开入口、异常类型与确定性结果全部保留：

1. `convert` / `eval_source` / `eval_target` 的签名、返回值、异常类型不变；
   `source_interp.py`、`target_interp.py` 仍是原来的递归实现。
2. AST 构造全部保持位置参数兼容；新增的 `ver=` 是关键字专用、默认 `None`。
3. `Program` 增加 `meta`、`TFunction` 增加 `capture_kinds/ver_tag/group/member`
   （均有缺省值）；旧代码不读取这些字段不受影响。
4. 新增错误类型都是 `RecenvError` 子类；旧的 `except RecenvError` 仍捕获全部。
5. 新快照格式使用独立格式标记 `recenv-snapshot-v1`，不与任何旧格式冲突；
   版本化能力必须显式使用（`ver=` / `Machine` / `MigrationRules`），默认关闭。
6. 旧测试一个未删、断言一条未放松（`tests/` 仍 60 个全过）。

唯一与“升级”相关的语义取舍是新能力内部的定义（旧栈帧跑旧代码、外部持有者
换新版本），旧执行路径不经过它，因此不存在需要旧调用适配的破坏性变更。

## 验收方式

* **独立对照**：期望值由不导入 `convert`/`machine` 的 `source_interp` 与手工
  小参考域常量给出；机器结果只用于对照。参考验证不调用被测求解器算期望值。
* **差分测试**：`tests/proggen.py` 固定种子生成 180+ 程序（含 `set` 副作用、
  二/三函数互递归、捕获外层 cell 的递归函数），源/目标/机器三方对拍结果、
  事件与精确步数。
* **逐步对照**：互递归、两个闭包共享计数器、迁移失败回滚都有逐步事件序列断言；
  连续执行与多次暂停/恢复的可见结果、异常位置、单元身份关系一致。
* **规模**：扩展测试实测 ≥1100 堆对象、200 个内容各异的函数版本、600+ 显式
  调用帧（在 `recursionlimit=200/300` 下），不是玩具输入。
* **端到端**：`tests_ext/test_e2e.py` 与 `demo.py` 同一条流水线上同时用到
  暂停→新进程恢复→版本迁移→新闭包改共享 cell→再暂停→再恢复→旧栈跑完。

## 设计取舍与已知限制

* 语言子集仍为：整数 + 加法 + 闭包 + if0 + set + 同时绑定 letrec；没有乘法、
  字符串、数据结构、I/O。不宣称与任何行业标准字节码/ABI 兼容。
* 版本化只覆盖带 `ver=` 的函数/组；未加标签的函数不参与迁移（这是有意的显式
  选择，避免对不可见代码做隐式升级）。
* 新增字段只能是不可变常量 `int`；不支持按实例计算默认值或 cell 默认值
  （题目允许的迁移规则之外的一律拒绝，而不是猜测）。
* 安全点固定为调用入口；表达式内部不暂停（暂停粒度为一次调用边界）。
* 快照为 JSON 文本，面向正确性与可核对性做了规范化，未做压缩或二进制编码。
* `source_interp`/`target_interp` 保留递归下降形态用于对照；显式栈机器
  `machine.Machine` 才是不依赖 Python 递归栈的执行器。

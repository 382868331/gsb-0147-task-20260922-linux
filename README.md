# recenv — 递归闭包的环境布局转换（含可暂停 / 可恢复 / 可热升级执行）

为离线编译器中间表示工程提供的可复用模块。第一部分（原契约）把带词法作用域、
闭包、可变变量和同时绑定递归函数组的小型源语言，**闭包转换**为无自由变量的
函数表 + 显式环境构造 + 主表达式，配套两个独立解释器对照验收。

第二部分（本次迭代）在同一份数据模型与公开入口上加入：

1. **可暂停 / 可保存 / 跨进程恢复的显式栈执行机**：运行态不依赖 Python 递归栈，
   环境对象（记录 / cell / 闭包）全部带稳定 OID 放在显式堆上；
2. **对象图序列化格式 `recenv-image/1`**：两阶段装载，完整保留共享、别名与环
   （互递归组帧 cell ↔ 成员闭包），绝不把别名复制成不同对象；
3. **函数版本表 + 显式字段迁移**：只允许重命名、删除未被引用字段、增加带
   常量默认值字段；恢复/升级前完整验证引用图与代码版本，迁移成功才发布；
4. **显式安全点与步数预算**：可在调用栈中间保存续执行状态；版本升级只发生在
   调用入口安全点，旧栈帧继续执行其代码版本，新调用使用新版本。

- Python 版本：**3.14.7**（仅标准库：`dataclasses` / `json` / `random` /
  `unittest` / `subprocess` / `sys`）
- 依赖：**无第三方依赖**；网络 / 账号 / 密钥 / 数据库 / 容器 / 云 API 均不需要
- 子进程以最小环境（`PATH` + 包路径 `PYTHONPATH`）启动，不读取环境变量清单

## 运行命令

```bash
# 快速回归（默认；104 个测试，约 2 秒）
python -m unittest discover -s tests -v

# 扩展规模验证（门控；3 个测试，约 1.5 秒；覆盖 1000 对象 / 200 版本 / 500 帧）
RUN_RECENV_EXTENDED=1 python -m unittest tests.test_scale -v

# 固定输入演示（约 2.5 秒，上限 90 秒；真实计算，无 sleep、无预录结果；
# 含真正的新进程接力恢复）
python demo.py
```

两条测试入口均完全离线。快速回归与扩展验证通过环境变量显式分开。

## 包结构

```
recenv/
  ast_nodes.py     源 AST + 目标 IR（frozen dataclass）；新增 FieldSpec 与
                   Program 的 frame_schemas/function_field_map/code_version
  errors.py        错误类型（新增 ImageFormatError / MachineStateError /
                   UpgradeValidationError / UpgradeAborted）
  resolver.py      静态解析：绑定 ID、遮蔽、可变性、自由变量（未改动）
  convert.py       闭包转换 + 显式环境布局（未改动；输出固定 code_version="v1"）
  source_interp.py 独立源解释器（参考实现；不导入 convert；未改动）
  target_interp.py 旧递归下降目标解释器（保留为独立参考；未改动）
  ircode.py        IR / Program 的结构化 JSON 编解码（镜像代码段）
  heap.py          显式堆：Record / Cell / Closure + 稳定 OID 与可达性
  codetable.py     多版本函数表（不可变版本条目 + current 指针）
  machine.py       显式栈归约机：暂停/恢复、安全点、冻结版本视图
  migration.py     fieldize 字段化；安全点升级；三类字段迁移；暂存/回滚
  image.py         recenv-image/1 序列化与完整引用图校验（两阶段装载）
  persist.py       镜像文件编排；run_segment 启动全新 python 进程恢复
  _worker.py       python -m recenv._worker（跨进程恢复入口）
tests/
  test_ast_validation.py / test_semantics.py / test_differential.py
  test_limits.py / test_structures.py     原有 60 个测试，断言未放松
  test_machine_semantics.py  执行机对拍、暂停恢复、异常位置、600 层显式栈
  test_persistence.py        环/共享/别名、置换不变性、损坏拒绝、真跨进程
  test_upgrade.py            安全点升级故事、互递归、迁移拒绝、回滚、版本视图
  test_scale.py（门控）       1000 对象 / 200 版本 / 500 帧
  proggen.py                 确定性程序生成器（固定种子）
demo.py                      固定输入演示
```

## 公开接口

```python
from recenv import (
    # —— 原有接口（行为完全保留）——
    convert,                 # convert(Expr) -> Program（code_version="v1"）
    eval_source,             # (Expr, step_limit=10000) -> (int, events)
    eval_target,             # (Program, step_limit=10000) -> (int, events)
    errors,                  # 旧错误类型 + 新错误类型
    # —— 显式栈执行机 ——
    Machine,                 # 可暂停/恢复/升级的执行机
    run_machine,             # 便捷一次性入口 -> (int, events, Machine)
    # —— 镜像（recenv-image/1，独立格式）——
    fieldize,                # Program -> 带结构化字段声明的 Program（纯函数）
    dump_machine, load_machine,      # Machine <-> dict
    dump_image_json, load_image_json,
    save_image, load_image,
    upgrade_at_safepoint,    # 安全点原子升级（含注入失败回滚参数）
    IMAGE_FORMAT,            # "recenv-image/1"
)
from recenv import persist   # save_initial_image / run_segment（新进程）
```

### 执行机基本用法

```python
m = Machine(fieldize(convert(root)), step_limit=10_000)
m.run(step_budget=100)          # 暂停在“下一条待归约表达式”前（调用栈中间也行）
m.run()                         # 恢复到结束；m.result / m.events / m.steps
m.at_safepoint                  # 是否停在调用入口安全点
m.fid_trace()                   # [(fid, version), ...] 调用栈底在前
```

步数口径与旧目标解释器**逐拍一致**（TConst/TGet/TCellGet/TCellSet/TPrim/
TMkClosure/TCall/TIf0/TWithFrame 各计 1；TMkCell/TFillCell/TBegin 是布局簿记，
不计；闭包环境槽抽取不计）。差分测试同时核对源/旧目标/执行机的返回值、
副作用事件与精确步数。

### 跨进程恢复

```python
persist.save_initial_image(root, "ck.json", step_limit=10_000_000)
r = persist.run_segment("ck.json", step_budget=8000)
# r: {"status": "paused"|"finished"|"error", "result", "steps", "events", ...}
# 成功时用新镜像原子覆盖 ck.json；error 时旧检查点保留不动
```

## 镜像格式 `recenv-image/1`（新增，独立版本）

JSON 顶层：`{"format": "recenv-image/1", "code": {...}, "data": {...},
"control": {...}}`。

* **code**：Program（IR 树带标签对象）+ 多版本函数表（fid -> {version ->
  TFunction}）与 current 指针；栈帧**不携带代码副本**。
* **data**：堆对象以稳定 OID（`o0, o1, ...`）为键，引用一律是 OID 字符串；
  整数值直接内联（`int`/`bool` 严格区分），未填充 cell 用显式标记。
  - 装载分两阶段：先登记全部 record/cell/closure，再统一校验引用，因此
    **环和共享原样恢复**，与条目排列顺序无关（有 8 种以上置换的测试）；
  - 每条记录带 schema 标记（`capture:<fid>:<ver>` / `act:<fid>:<ver>` /
    `frame-schema:...`），装载时按对应代码版本的 n_capture/n_slots 逐条核对。
* **control**：显式控制栈（每帧 record OID、`(fid,version)` 代码坐标、
  当前表达式路径 `path`、续延 `k`、冻结版本视图 `versions_view`）、步数、
  事件、状态、结果。
  - 装载时从版本表取回代码体，沿 `path` 重新定位表达式，并与镜像里记录的
    节点标签交叉核对——**镜像不能夹带自己的代码来执行**；
  - 续延中的调用节点路径、参数下标、累加值、待绑定闭包 OID 全部重新校验；
  - 发布前做完整引用图校验：悬空引用、跨类型引用（闭包 record 指向 cell 等）、
    重复 OID、代码版本缺失、schema 长度不符、坐标越界一律 `ImageFormatError`。
  - 不可达对象允许保留（单调分配的堆日志语义），但其结构同样逐条校验；
    栈根引用不允许悬空。

检查器**不信任镜像或升级包内任何“我合法”的布尔自述**；例如 cell 的
filled/unfilled 不一致、record schema 与函数槽数不符都会被独立拒绝。

## 函数版本表与字段迁移

* 每个 fid 有不可变版本链（初始 `v1`）；栈帧与堆上闭包用 `(fid, version)`
  定位代码。版本条目只追加、不修改。
* **冻结版本视图**：激活帧在调用时刻快照 current 表；该帧（及其内层
  TWithFrame 控制帧）之后构造任何闭包都按快照绑定版本。升级后：
  旧栈帧及其创建的闭包仍是旧版本（旧捕获槽数），新压入的激活帧看到新版本，
  环境引用不会跨版本混淆。有专门的测试（旧帧造 v1 闭包、新帧造 v2 闭包、
  两者共享同一 cell OID）锁定此语义；把实现改回“全局 current 解析”该测试
  立即失败。
* **升级只允许在安全点**（调用入口，激活帧刚压入、函数体尚未归约；或机器
  已结束）。非安全点调用抛 `MachineStateError`。
* 字段操作（按结构化字段名，槽位只是位置）：
  | 操作 | 规则 |
  | --- | --- |
  | `rename` | 仅捕获槽；参数槽是调用 ABI，禁止改名 |
  | `delete` | 仅捕获槽；新代码字段布局必须不再包含该字段（按名+槽双重校验） |
  | `add`    | 必须带常量整数 `default`；迁移时给存活闭包捕获记录追加该常量槽 |
  参数列表与参数可变性跨版本不得改变；不允许凭空新增 fid（只能升级已有函数）。
* **原子性**：在暂存副本（堆深拷贝 + 版本表结构拷贝 + 程序副本）上完成验证、
  新版本登记、捕获记录迁移与闭包载荷同 OID 重发；任一环节失败（含测试注入
  `inject_failure="validate"|"stage"|"commit"`）都丢弃暂存区，版本指针、
  闭包载荷、记录布局、栈帧版本全部不变。回滚测试对真实状态逐项独立复查，
  不信任异常自带的 `rolled_back` 标志。

同一执行例内的关键组合（`test_upgrade.py` 与 `demo.py` 第 5 节逐步展示）：
旧 v1 栈帧持有共享计数器 cell → 升级后迁移的 v2 闭包在**同一 cell OID** 上
再次写入 → 在第一个 v2 入口安全点中断 → 恢复到结束；事件序列、返回值、
单元身份关系都确定。

## 输入上限与错误语义

| 边界 | 值 | 超限行为 |
| --- | --- | --- |
| 源 AST 节点数 | **500** | `NodeLimitError(count, limit)` |
| 默认执行步数 | **10_000**（执行机/持久化可传更大 `step_limit`） | `StepLimitError(steps, limit, events, fid_trace)` |
| 契约规模 | ≥ **1000 环境对象 / 200 函数版本 / 500 显式调用帧** | `tests/test_scale.py` 实测覆盖（1000+ 对象、201 版本条目、500+ 帧） |
| 整数字面量 | int（bool 拒绝）；有限整值 float 接受 | `ASTError` |
| 名字 | 非空字符串；参数/letrec 组内不重名 | `ASTError` |
| 镜像格式 | 必须为 `recenv-image/1`；引用图/代码版本/坐标完整校验 | `ImageFormatError` |
| 升级位置 | 仅调用入口安全点 | `MachineStateError` |
| 字段迁移 | 仅 rename / 删未引用 / 加常量默认；参数表不变 | `UpgradeValidationError`（发布前拒绝，无部分变更） |

旧有静态/运行期错误类型与消息前缀（`"<loc>: "`）保持不变；执行机抛出的
`LangRuntimeError` 额外附带 `fid_trace`（`[(fid, version)]`）与 `ir_node`
（出错时调用点 IR 节点类型），用于校验“连续执行 vs 分段恢复”的异常位置一致；
旧解释器不设置这两个属性。

## 确定性与参考验证

* 同一输入（含相同暂停预算序列）重复执行得到同一结果、同一事件序列、同一
  步数与同一堆 OID 图；测试断言不同暂停切分跑到同一总步数时堆数据段逐字节
  结构相同。
* **小规模参考域完整核对**：手工语义用例（计数器共享、互递归、遮蔽、可变
  参数、未绑定等）+ 固定种子差分（120 普通程序 + 60 letrec + 三函数互递归），
  期望值全部来自独立源解释器 `source_interp`（不导入 convert）；参考不调用
  被测求解器。
* 扩展规模另给非退化例子：260 层闭包工厂（1000+ 环境对象）、200 次连续安全
  点热升级（201 个共存代码版本、栈底到栈顶版本各不同）、500 层递归在调用栈
  中间序列化往返。
* 本语言只有任意精度整数与加法，没有浮点结果与误差区间；float 仅在构造
  `Int` 时按“有限整值”接受，接受后即转为精确 `int`。没有可满足性搜索，
  因此不存在 unknown/未决；预算耗尽（`StepLimitError`）与静态确定的错误严格
  分开，且前者携带耗尽前事件。对“认证/最优/合法性”一类判断，检查器一律
  自行重算（引用槽扫描、字段名比对、引用图遍历），不读取任何自报标志。

## 与旧行为的差异及兼容办法

本次迭代**未改变任何旧接口的行为**（60 个旧测试原样通过），新增能力通过
独立格式与显式选项进入，不需要迁移旧调用：

1. `Program` 新增 4 个带默认值字段（`frame_schemas={}`、
   `function_field_map={}`、`code_version="v1"`、`field_defaults={}`）；
   `TWithFrame` 新增 kw-only 默认 `schema_id=None`。旧的位置构造方式全部
   不受影响；直接构造 `Program(...)` 的旧代码无需改动。
2. 持久化/升级要求先 `fieldize(program)`（纯函数，给帧槽补结构化字段名，
   字段名取自原 `layout` 标签的限定名 `name#idN`，不改变任何运行语义）。
   对未字段化机器调用 `dump_machine` 会明确抛 `MachineStateError` 而不是
   产出不完整镜像。`persist.save_initial_image` 接受源 AST 或 Program，
   内部自动 convert + fieldize。
3. 旧的递归下降目标解释器 `eval_target` 保留为独立参考实现；新执行机
   `Machine` 与它共享 IR 数据模型但不共享运行态结构。两者在差分用例上
   返回值、事件、步数逐项一致。
4. 执行机的 `LangRuntimeError` / `StepLimitError` 多出 `fid_trace` 等属性，
   纯增量，旧的 `type(e)`、`str(e)`、`events` 用法不变。

## 设计取舍与已知限制

* 版本升级的**字段迁移作用于存活闭包的捕获记录**：`add` 只能填常量整数
  （契约如此），不能把某个既有共享 cell OID 作为“新捕获字段”注入；
  需要共享可变状态时，被共享 cell 必须在 v1 捕获布局中已存在
  （演示与互递归升级用例均按此建模）。
* `delete`/`rename` 只针对捕获槽；参数槽跨版本固定（调用 ABI 稳定）。
* 新代码以手工/编译器生成的 `TFunction`（IR）提供，字段名经 `free_names`
  与迁移计划逐槽核对；本模块不提供“源 AST -> 指定版本 IR”的再编译入口，
  因为契约只要求版本表与显式迁移规则。
* 堆是单调分配日志：出帧对象不主动回收，镜像可能包含不可达历史对象
  （结构仍完整校验）；对 500 帧 / 1000 对象量级实测无压力，但未实现 GC。
* 持久化格式只覆盖本语言运行态（int / record / cell / closure），不含 Python
  任意对象；镜像必须是受支持的 JSON 形状，装载器不接受额外类型。
* 短演示不穷举上限；500 节点 / 10000 步与 1000 对象 / 200 版本 / 500 帧
  分别由快速回归和门控扩展测试精确覆盖。

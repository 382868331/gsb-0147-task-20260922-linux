# closure_layout — 递归闭包的环境布局转换

为一个离线编译器中间表示工程实现的可复用模块：把含递归闭包的源语言
转换为**无自由变量的函数表 + 显式环境构造 + 主表达式**，并附带两个
小解释器用于独立对照验证。仅使用 Python 3.14 标准库，无第三方依赖。

## 运行环境与命令

- Python：**3.14.7**（仅用标准库；3.10+ 的语法亦可运行，未在其他版本验证）
- 无需安装任何依赖，无需网络 / 账号 / 数据库 / Docker。

在仓库根目录执行：

```bash
python -m unittest discover -s tests -v   # 必要测试
python demo.py                            # 固定输入演示（约 0.1 秒）
```

## 源语言

严格从左到右求值的小型词法作用域语言（s 表达式）：

| 形式 | 含义 |
| --- | --- |
| 整数（任意精度，如 `-1`、`100`） | 整数字面量 |
| `(+ E E)` | 整数加法，先左后右 |
| `x` | 变量引用 |
| `(set x E)` | 赋值，值为所赋的值 |
| `(if0 E E E)` | 条件为 0 取第二式，否则取第三式 |
| `(lambda (x ...) E)` | 函数字面量 |
| `(E E ...)` | 调用，函数位置先求值，实参从左到右 |
| `(let (x E) E)` | 顺序绑定（右值在扩展前求值，不可自引用） |
| `(letrec ((x (lambda ...)) ...) E)` | **同时绑定**的互递归函数组 |

程序顶层结果必须是整数。本语言**没有浮点**，因此不存在浮点误差：
整数为 Python 任意精度整数，加法精确，无任何容差或舍入。

## 公开接口

```python
from closure_layout import parse, convert, run, run_source

tree    = parse(text)          # str -> 已校验 AST（报错带行列）
program = convert(tree)        # AST -> Program（函数表 + 显式环境 + main）
r1      = run_source(tree)     # 独立源参考解释器：不经过转换核心
r2      = run(program)         # 执行转换后的 IR（== run_target）

r1.value          # 整数返回值
r1.steps          # 实际执行步数
r1.trace          # [(set 表达式编号, 所赋整数值 / 非整数标记), ...]
```

也可用 `closure_layout.ast` 的构造器手工建 AST：`int_ / add_ / var_ /
set_ / if0_ / lambda_ / call_ / let_ / letrec_`。

### 输出数据结构

`convert` 返回 `closure_layout.ir.Program`：

- `functions: dict[str, Function]`：函数表。每个 `Function` 含
  - `free_vars`：按**绑定 ID 升序**的自由变量（绝不按名字排序）；
  - `outer_free` / `group_size` / `boxed_params`：组帧与装箱信息；
  - `body`：目标 IR，只含局部槽 `TLocal`、闭包环境槽 `TGet`、
    组帧 `TGetGroup/TFrameGet`、cell `TCellGet` 等显式访问，
    **没有任何裸名字，也没有未消解的自由变量**；
- `main_fid = "main"`：主表达式所在函数；
- `binding_info: bid -> (名字, 是否装箱)`。

目标 IR 节点见 `closure_layout/ir.py`；可读布局用
`closure_layout.layout.render(program)`。

## 环境布局设计

1. **绑定 ID**：每个绑定（形参 / let / letrec 组成员）在结构遍历时
   获得唯一 ID，同名遮蔽得到不同 ID。自由变量集合按 ID 排序，闭包
   环境向量据此布槽，因此同名遮蔽不会串扰。
2. **共享 cell（可变捕获）**：仅当一个形参/let 绑定**既被 `set`
   赋值、又被某个 lambda 捕获**时，才在绑定点装入共享 cell；
   只读捕获按值布槽，避免无谓间接。装箱形参在函数入口把实参包进
   cell 并改指新局部槽，调用约定始终传原始值（动态调用不会错配）。
3. **互递归共享组帧**：每个 `letrec` 建立一个含 N 个未初始化槽的
   组帧（`TMakeGroup`）；N 个成员闭包的环境槽 0 都指向**同一**组帧，
   成员体内互访经环境槽 0（`TGetGroup`）。组帧也是一等值：宿主与
   外部捕获者通过普通环境槽持有它（`TFrameGet/TFrameSet`），因此
   嵌套闭包捕获组函数、在组外读取/重绑组函数均可工作。
4. **自由变量闭包传递**：内层/成员函数的自由变量在后序折叠中向上
   传播到构造闭包的宿主函数，由宿主在闭包构造点（`TClosure`）显式
   提供等长环境向量。
5. **目标解释器**是显式栈抽象机（非 Python 递归），10000 步预算内
   任意嵌套深度的调用都能执行。

## 输入上限与错误语义

- AST 节点数上限 **500**（`closure_layout.ast.MAX_NODES`），超出抛
  `NodeLimitError`（转换与源参考解释器对称执行）。
- 执行步数上限 **10000**（两个解释器默认；可用 `max_steps=` 调整），
  超出抛 `StepLimitError`——这是**预算耗尽**，与“已证明的程序错误”
  （未绑定变量等）是不同异常类型。
- 整数字面量拒绝 `bool`；float 一律拒绝（`NaN`/`Infinity` 给明确
  错误信息）；非字符串源码、非 Node 子节点、空括号、括号不配对、
  顶层多形式、形参/letrec 重名、letrec 右值非 lambda 等均为
  `ValidationError`/`LetrecFormError`。
- 词法未绑定变量（含永不执行的死分支中的引用）在执行前静态拒绝，
  错误携带 1 起始行列：`UnboundVariableError`。
- 运行期错误：以非函数调用 / 非整数相加 / `if0` 条件非整数 →
  `LangTypeError`；实参数不符 → `ArityError`。另设
  `UninitializedSlotError` 作为组帧/槽位的防御性不变量；在本题
  “letrec 右值必须是 lambda、组体求值前完成填充”的契约下，合法
  程序不会读到未初始化槽。
- 转换是纯函数（不修改输入 AST）；解释器运行不改变 AST，故失败不
  产生需要回滚的部分变更。
- 错误只报告，不做静默纠正或猜测补全。

## 测试与验证方式

- `tests/test_validation.py`：字面量/结构校验、定位、500 节点边界。
- `tests/test_conversion.py`：自由变量按 ID 排序、装箱判定、共享
  cell/组帧结构、无自由变量残留。
- `tests/test_interpreters.py`：手写期望常量（返回值）、副作用序列、
  闭包共享计数、互递归、同名遮蔽，以及用仓库内**确定性生成器**
  `tests/_proggen.py`（固定种子）批量生成程序做差分对照。
- `tests/test_limits.py`：节点上限、步数预算、预算耗尽与程序错误的
  类型区分、运行期类型/元数错误的两侧对称。

独立性说明：`closure_layout/source_ref.py` 是独立参考实现，只把 AST
节点当数据，自行实现词法检查、作用域、set 编号与求值，**不 import**
`scope / convert / target`；测试期望值来自它或手写常量，不调用被测
转换核心来产生期望值。“源 AST → 转换 → 目标执行”的往返一致作为
补充断言。开发期另用约万级随机程序做了差分模糊测试（值与副作用
序列、错误类别全部一致），该脚本未纳入交付测试。

## 设计取舍与已知限制

- 只实现本题规定的有限语言子集，不宣称与任何行业标准语言兼容；
  无浮点、字符串、布尔值、复合数据、I/O。
- `letrec` 右值按契约限定为 lambda；不支持任意右值的递归绑定。
- 副作用轨迹中，被赋值为闭包/组帧等非整数时统一记录标记
  `closure_layout.target.NONINT_TRACE`（两侧同值），因为轨迹只承载
  整数赋值；程序语义本身不受影响。
- 目标机“步”是单条控制指令（粒度比源解释器的“一次求值”细），两侧
  步数数值不要求相等，只要求在同一 10000 接口预算语义下终止行为
  一致；两侧各自在超预算时抛同一类 `StepLimitError`。
- 源参考解释器求值期间临时把 Python 递归上限提高到 100000，结束后
  恢复原值；目标解释器不受 Python 调用栈深度影响。

## 文件清单

```
closure_layout/
  __init__.py     公开导出
  errors.py       错误类型
  ast.py          源 AST、构造器、结构校验、节点计数
  reader.py       s 表达式读取（带行列）
  scope.py        绑定 ID / 作用域 / 自由变量 / 装箱分析
  ir.py           目标中间表示（函数表、显式环境、组帧、cell）
  convert.py      convert(AST) 环境布局转换核心
  target.py       目标 IR 解释器（显式栈、步数预算）
  source_ref.py   独立源参考解释器（不依赖被测核心）
  layout.py       环境布局可读展示
  api.py          门面接口
demo.py           固定输入演示
tests/            unittest 测试与确定性程序生成器
```

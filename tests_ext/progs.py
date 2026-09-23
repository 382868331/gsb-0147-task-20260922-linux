"""扩展测试共用的确定性带版本程序构造器。

所有程序都在仓库内确定性生成，期望值（返回值、事件序列、字段槽）由构造器
旁的手工小参考域给出——不通过被测机器反推。
"""

from __future__ import annotations

from recenv import ast_nodes as ast


# ---------------------------------------------------------------------------
# 独立版本函数 bump（捕获一个共享可变 cell），由 letrec drive 驱动
# ---------------------------------------------------------------------------

def standalone_v1(n: int) -> ast.Expr:
    # let c = 0 in
    # let bump@"bump@1"(x) = (() -> set c = c + x)() in   # IIFE 入口是安全点：
    # letrec drive(n) = if0 n then 0 else bump(1) + drive(n-1)
    # in drive(n) + c
    #
    # bump 帧在 IIFE 调用入口已激活（持有共享 cell），但对 c 的读发生在 IIFE
    # 内部、安全点之后——因此在该安全点迁移并旁路运行新闭包后，旧 bump 帧会
    # 观察到新闭包写入的值（而不是它入帧时的旧值）。
    bump = ast.Lambda(
        ["x"],
        ast.Call(
            ast.Lambda([], ast.Set("c", ast.Add(ast.Var("c"), ast.Var("x")))),
            [],
        ),
        ver="bump@1",
    )
    drive = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"),
            ast.Int(0),
            ast.Add(
                ast.Call(ast.Var("bump"), [ast.Int(1)]),
                ast.Call(ast.Var("drive"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        ),
    )
    return ast.Let(
        "c", ast.Int(0),
        ast.Let(
            "bump", bump,
            ast.LetRec(
                [ast.RecBinding("drive", drive)],
                ast.Add(ast.Call(ast.Var("drive"), [ast.Int(n)]), ast.Var("c")),
            ),
        ),
    )


def standalone_v2(n: int, bias: int = 100) -> ast.Expr:
    # 字段：c 重命名为 kount；新增常量字段 bias。bump@2 写 kount+ x + bias。
    bump = ast.Lambda(
        ["x"],
        ast.Set(
            "kount",
            ast.Add(
                ast.Add(ast.Var("kount"), ast.Var("x")),
                ast.Var("bias"),
            ),
        ),
        ver="bump@2",
    )
    drive = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"),
            ast.Int(0),
            ast.Add(
                ast.Call(ast.Var("bump"), [ast.Int(1)]),
                ast.Call(ast.Var("drive"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        ),
    )
    return ast.Let(
        "bias", ast.Int(bias),
        ast.Let(
            "kount", ast.Int(0),
            ast.Let(
                "bump", bump,
                ast.LetRec(
                    [ast.RecBinding("drive", drive)],
                    ast.Add(
                        ast.Call(ast.Var("drive"), [ast.Int(n)]),
                        ast.Var("kount"),
                    ),
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 互递归组 eo {ev, od}，两成员共享外层 cell c；ev 写 c，ev/od 互递归
# ---------------------------------------------------------------------------

def mutual_v1(n: int) -> ast.Expr:
    ev = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"),
            ast.Int(1),
            ast.Add(
                ast.Set("c", ast.Add(ast.Var("c"), ast.Int(1))),
                ast.Call(ast.Var("od"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        ),
    )
    od = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"),
            ast.Int(0),
            ast.Call(ast.Var("ev"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    return ast.Let(
        "c", ast.Int(0),
        ast.LetRec(
            [ast.RecBinding("ev", ev), ast.RecBinding("od", od)],
            ast.Add(ast.Call(ast.Var("ev"), [ast.Int(n)]), ast.Var("c")),
            ver="eo@1",
        ),
    )


def mutual_v2(n: int, bias: int = 1000) -> ast.Expr:
    # c 重命名为 kount；ev 新增常量字段 bias；od 只经 rec 槽引用伙伴，
    # 因此没有外部字段。
    ev = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"),
            ast.Int(1),
            ast.Add(
                ast.Set(
                    "kount",
                    ast.Add(
                        ast.Add(ast.Var("kount"), ast.Int(1)),
                        ast.Var("bias"),
                    ),
                ),
                ast.Call(ast.Var("od"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
            ),
        ),
    )
    od = ast.Lambda(
        ["n"],
        ast.If0(
            ast.Var("n"),
            ast.Int(0),
            ast.Call(ast.Var("ev"), [ast.Add(ast.Var("n"), ast.Int(-1))]),
        ),
    )
    return ast.Let(
        "bias", ast.Int(bias),
        ast.Let(
            "kount", ast.Int(0),
            ast.LetRec(
                [ast.RecBinding("ev", ev), ast.RecBinding("od", od)],
                ast.Add(ast.Call(ast.Var("ev"), [ast.Int(n)]), ast.Var("kount")),
                ver="eo@2",
            ),
        ),
    )


# 手工小参考域（mutual_v1，供测试对照）：
#   ev(n) 沿 ev(n)->od(n-1)->ev(n-2) 递减，仅在 ev 的奇数实参上 set c（+1）。
#   c 的最终值 = 从 n 起每次 -2 的正数个数 = (n+1)//2；
#   ev(5)=6（1+三个 set 值 1/2/3），加 c=3，main = 9。
EO_C_FINAL = {5: 3}
EO_MAIN_RESULT = {5: 9}
EO_EVENTS_5 = [("set", "c", 1), ("set", "c", 2), ("set", "c", 3)]

"""确定性测试程序生成器（仓库内置，无外部依赖、无网络）。

只用固定种子的 :class:`random.Random` 生成**词法合法**的源程序文本：

- 变量只引用当前作用域可见名字；
- 故意复用少量名字制造同名遮蔽；
- letrec 递归良基（形参 t，递归调用实参 (+ t -1)，起始为小常量）；
- 规模受控（深度/常量有界），在默认 10000 步预算内结束。

本模块不 import 被测转换核心，只产生字符串（期望值由独立源解释器
在测试中另行计算）。
"""

from __future__ import annotations

import random

_VAR_NAMES = ("x", "y", "z", "n", "k")
_FN_LETTERS = ("a", "b", "c")


class ProgramGenerator:
    def __init__(self, seed: int) -> None:
        self.r = random.Random(seed)
        self._uniq = 0

    def _fresh_param(self) -> str:
        self._uniq += 1
        return f"p{self._uniq}"

    def expr(self, depth: int, env: list[str]) -> str:
        r = self.r
        # depth 越小越倾向叶子，保证规模与步数有界。
        choices = 10 if depth > 0 else 3
        c = r.randrange(choices)

        if c == 0:
            return str(r.randrange(-3, 4))
        if c == 1:
            return r.choice(env) if env else str(r.randrange(5))
        if c == 2:
            return (f"(+ {self.expr(depth - 1, env)} "
                    f"{self.expr(depth - 1, env)})")
        if c == 3 and env:
            return f"(set {r.choice(env)} {self.expr(depth - 1, env)})"
        if c == 4:
            return (f"(if0 {self.expr(depth - 1, env)} "
                    f"{self.expr(depth - 2, env)} "
                    f"{self.expr(depth - 2, env)})")
        if c == 5:
            # 立即调用 lambda（含被内层闭包 set 的形参 -> 装箱形参）。
            nargs = r.randrange(0, 3)
            params = [self._fresh_param() for _ in range(nargs)]
            args = " ".join(self.expr(depth - 1, env) for _ in range(nargs))
            body = self.expr(depth - 1, env + params)
            return (f"((lambda ({' '.join(params)}) {body}) {args})")
        if c == 6 and env:
            # 高阶调用：传入捕获当前变量的闭包。
            p = self._fresh_param()
            return (f"((lambda (g) (g {r.choice(env)})) "
                    f"(lambda ({p}) (+ {p} {r.choice(env)})))")
        if c == 7:
            # let；一半概率复用名字制造遮蔽。
            if r.randrange(2):
                name = r.choice(_VAR_NAMES)
            else:
                name = self._fresh_param()
            return (f"(let ({name} {self.expr(depth - 1, env)}) "
                    f"{self.expr(depth - 1, env + [name])})")
        if c == 8:
            # 命名闭包：let 绑定 lambda 后调用，可能 set 其捕获变量。
            name = r.choice(_VAR_NAMES)
            nargs = r.randrange(0, 2)
            params = [self._fresh_param() for _ in range(nargs)]
            args = " ".join(self.expr(depth - 2, env) for _ in range(nargs))
            # 注意：let 右值在扩展前求值，lambda 体内不能引用 name
            # 自身（自递归只由 letrec 表达）。
            lam = (f"(lambda ({' '.join(params)}) "
                   f"{self.expr(depth - 2, env + params)})")
            return f"(let ({name} {lam}) ({name} {args}))"
        if c == 9:
            return self._letrec(depth, env)
        return str(r.randrange(5))

    def _letrec(self, depth: int, env: list[str]) -> str:
        r = self.r
        k = r.randrange(1, 4)
        fnames = [f"{_FN_LETTERS[i % len(_FN_LETTERS)]}rec" for i in range(k)]
        binds: list[str] = []
        for i in range(k):
            other = fnames[(i + 1) % k]
            rec = f"({other} (+ t -1))"
            # 递归步：1 + 互递归调用，可再叠加一个可见自由变量。
            step = f"(+ 1 {rec})"
            if env and r.randrange(2):
                step = f"(+ {r.choice(env)} {step})"
            body = f"(if0 t 0 {step})"
            binds.append(f"({fnames[i]} (lambda (t) {body}))")
        start = str(r.randrange(0, 4))  # 小的非负初值，保证终止且省步数
        return (f"(letrec ({' '.join(binds)}) "
                f"{self.expr(depth - 2, env + fnames)})"
                if r.randrange(2)
                else f"(letrec ({' '.join(binds)}) ({fnames[0]} {start}))")


def generate(seed: int, count: int, *, depth: int = 5) -> list[str]:
    gen = ProgramGenerator(seed)
    return [gen.expr(depth, []) for _ in range(count)]

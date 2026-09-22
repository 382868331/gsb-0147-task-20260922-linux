"""确定性源程序生成器（仅测试/演示使用）。

用固定随机种子递归生成符合本语言子集的程序。生成器自底向上回报*实际*
分配的节点数，父节点按剩余预算挑选构造，保证节点数严格不超过预算。

保证：
* 生成的 Var/Set 一定引用当前作用域内的名字（未绑定场景由专门用例手工构造）；
* lambda 调用元数匹配；
* letrec 组为自递归/互递归函数，参数沿首参 -1 递减，必然终止；
* 名字带全局唯一后缀，不会意外遮蔽。
"""

from __future__ import annotations

import random

from recenv import ast_nodes as ast


def _i(v, loc=None):
    return ast.Int(v, loc=loc)


def _v(n, loc=None):
    return ast.Var(n, loc=loc)


def _add(a, b, loc=None):
    return ast.Add(a, b, loc=loc)


def _let(n, e, b, loc=None):
    return ast.Let(n, e, b, loc=loc)


def _lam(ps, b, loc=None):
    return ast.Lambda(ps, b, loc=loc)


def _call(f, args, loc=None):
    return ast.Call(f, tuple(args), loc=loc)


def _if0(c, t, e, loc=None):
    return ast.If0(c, t, e, loc=loc)


def _set(n, v, loc=None):
    return ast.Set(n, v, loc=loc)


def _rb(n, f, loc=None):
    return ast.RecBinding(n, f, loc=loc)


def _letrec(bs, body, loc=None):
    return ast.LetRec(tuple(bs), body, loc=loc)


class Generator:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self._uid = 0

    def _uname(self, prefix: str) -> str:
        self._uid += 1
        return f"{prefix}_{self._uid}"

    def gen_program(self, max_nodes: int = 200) -> ast.Expr:
        expr, used = self._gen([], {}, 0, max_nodes)
        assert used <= max_nodes, (used, max_nodes)
        return expr

    # ------------------------------------------------------------------
    # 返回 (expr, 实际节点数)
    # ------------------------------------------------------------------

    def _leaf(self, names):
        if names and self.rng.random() < 0.5:
            return _v(names[self.rng.randrange(len(names))][0], loc="g:var"), 1
        return _i(self.rng.randint(-5, 5), loc="g:int"), 1

    def _gen(self, names, rec_funs, depth, budget):
        if budget <= 1 or depth >= 8:
            return self._leaf(names)

        candidates = ["int", "add", "if0", "call", "lambda_call", "let"]
        if names:
            candidates.append("var")
        settable = [n for n in names if n[1]]
        if settable:
            candidates.append("set")
        if rec_funs:
            candidates += ["rec_call", "rec_call"]
        # 预算越小越倾向叶子：按 budget 过滤掉放不下的构造
        feasible = [c for c in candidates if self._min_cost(c) <= budget]
        if not feasible:
            return self._leaf(names)
        choice = self.rng.choice(feasible)
        return getattr(self, f"_gen_{choice}")(names, rec_funs, depth, budget, settable)

    @staticmethod
    def _min_cost(choice: str) -> int:
        return {
            "int": 1,
            "var": 1,
            "set": 2,          # Set + 至少一个叶子值
            "add": 3,          # Add + 两叶
            "if0": 4,          # If0 + 三叶
            "call": 3,         # Call + Lambda + body 叶（0 参时）
            "lambda_call": 5,  # Let + Lambda + Call + body 叶 + 0 参
            "let": 3,          # Let + value 叶 + body 叶
            "rec_call": 3,     # Call + Var + 至少一个参数叶
        }[choice]

    def _split(self, budget, n_children):
        """把剩余预算（扣除固定开销后）分给 n_children，每子至少 1。"""
        if n_children == 1:
            return [max(1, budget)]
        total = budget - n_children
        cuts = sorted(self.rng.randrange(total + 1) for _ in range(n_children - 1))
        parts = [cuts[0]]
        for a, b in zip(cuts, cuts[1:]):
            parts.append(b - a)
        parts.append(total - cuts[-1])
        return [p + 1 for p in parts]

    def _gen_int(self, names, rec_funs, depth, budget, settable):
        return _i(self.rng.randint(-5, 5), loc="g:int"), 1

    def _gen_var(self, names, rec_funs, depth, budget, settable):
        return _v(names[self.rng.randrange(len(names))][0], loc="g:var"), 1

    def _gen_add(self, names, rec_funs, depth, budget, settable):
        b1, b2 = self._split(budget - 1, 2)
        a, c1 = self._gen(names, rec_funs, depth + 1, b1)
        b, c2 = self._gen(names, rec_funs, depth + 1, b2)
        return _add(a, b, loc="g:add"), 1 + c1 + c2

    def _gen_if0(self, names, rec_funs, depth, budget, settable):
        bc, bt, be = self._split(budget - 1, 3)
        c, cc = self._gen(names, rec_funs, depth + 1, bc)
        t, ct = self._gen(names, rec_funs, depth + 1, bt)
        e, ce = self._gen(names, rec_funs, depth + 1, be)
        return _if0(c, t, e, loc="g:if0"), 1 + cc + ct + ce

    def _gen_let(self, names, rec_funs, depth, budget, settable):
        bv, bb = self._split(budget - 1, 2)
        value, cv = self._gen(names, rec_funs, depth + 1, bv)
        name = self._uname(f"x{depth}")
        body, cb = self._gen(
            names + [(name, True)], rec_funs, depth + 1, bb
        )
        return _let(name, value, body, loc="g:let"), 1 + cv + cb

    def _gen_set(self, names, rec_funs, depth, budget, settable):
        target = settable[self.rng.randrange(len(settable))][0]
        v, cv = self._gen(names, rec_funs, depth + 1, budget - 1)
        return _set(target, v, loc="g:set"), 1 + cv

    def _gen_call(self, names, rec_funs, depth, budget, settable):
        # 现场构造 0..2 参 lambda 并立即调用；参数偶发可被 set（覆盖可变参数 cell）
        arity = self.rng.randint(0, min(2, (budget - 3) // 2))
        pnames = [self._uname(f"p{depth}") for _ in range(arity)]
        # 固定开销：Call(1) + Lambda(1) + body 与各参数
        n_children = 1 + arity
        parts = self._split(budget - 2, n_children)
        inner = [(p, self.rng.random() < 0.4) for p in pnames] + names
        body, cb = self._gen(inner, rec_funs, depth + 1, parts[0])
        args = []
        used_args = 0
        for i in range(arity):
            a, ca = self._gen(names, rec_funs, depth + 1, parts[1 + i])
            args.append(a)
            used_args += ca
        lam = _lam(tuple(pnames), body, loc="g:lam")
        return _call(lam, args, loc="g:call"), 2 + cb + used_args

    def _gen_lambda_call(self, names, rec_funs, depth, budget, settable):
        # let fname = lambda(...) in fname(args)；非递归 let，函数内不见自身
        arity = self.rng.randint(0, min(2, max(0, (budget - 5) // 2)))
        fname = self._uname(f"fn{depth}")
        pnames = [self._uname(f"q{depth}") for _ in range(arity)]
        n_children = 1 + arity  # lambda body + args
        parts = self._split(budget - 3, n_children)
        lam_scope = [(p, False) for p in pnames] + names
        fbody, cb = self._gen(lam_scope, rec_funs, depth + 1, parts[0])
        args = []
        used_args = 0
        for i in range(arity):
            a, ca = self._gen(names, rec_funs, depth + 1, parts[1 + i])
            args.append(a)
            used_args += ca
        lam = _lam(tuple(pnames), fbody, loc="g:lam2")
        call = _call(_v(fname, loc="g:fv"), args, loc="g:fc")
        return _let(fname, lam, call, loc="g:letfn"), 3 + cb + used_args

    def _gen_rec_call(self, names, rec_funs, depth, budget, settable):
        fname = self.rng.choice(sorted(rec_funs))
        arity = rec_funs[fname]
        # Call(1) + Var(1) + arity 个参数
        avail = budget - 2
        arity = min(arity, avail)
        # arity 被裁短时不应发生（生成组时参数少），兜底按实际 arity：
        if arity < rec_funs[fname]:
            # 预算不足：退化为叶子
            return self._leaf(names)
        parts = self._split(avail, arity) if arity else []
        args = []
        used = 0
        for i, pb in enumerate(parts):
            a, ca = self._gen(names, rec_funs, depth + 1, pb)
            args.append(a)
            used += ca
        return _call(_v(fname, loc="g:rv"), args, loc="g:rc"), 2 + used

    # ------------------------------------------------------------------
    # letrec 互递归组
    # ------------------------------------------------------------------

    def gen_letrec_program(self, max_nodes: int = 240, n_funs: int = 2) -> ast.Expr:
        names_funs = [self._uname("rec") for _ in range(n_funs)]
        arities = [self.rng.randint(1, 2) for _ in range(n_funs)]
        rec_funs = dict(zip(names_funs, arities))

        # 外层可变计数器 tick：递归函数偶尔 set 它，事件序列因此非空，
        # 同时覆盖“递归闭包捕获外层 cell 并修改”的组合。
        tick = self._uname("tick")
        # 预算：Let tick(2 节点) + LetRec(1) + 每绑定 RecBinding(1)+Lambda(1)
        #       + 主体 Call(1)+Var(1)+参数
        fixed = 2 + 1 + 2 * n_funs + 2
        arg_budget = arities[0]
        body_budget = max_nodes - fixed - arg_budget
        per_fun = max(4, body_budget // n_funs)

        bindings = []
        for i, (fname, arity) in enumerate(zip(names_funs, arities)):
            pnames = [self._uname(f"{fname}_a") for _ in range(arity)]
            scope = [(p, False) for p in pnames] + [(tick, True)]
            body, used = self._rec_body(
                names_funs, arities, scope, tick, rec_funs, 0, per_fun
            )
            bindings.append(
                _rb(fname, _lam(tuple(pnames), body, loc="g:rlam"))
            )

        main_args = [
            _i(self.rng.randint(0, 4), loc="g:marg") for _ in range(arities[0])
        ]
        main = _call(_v(names_funs[0], loc="g:mf"), main_args, loc="g:main")
        group = _letrec(bindings, main, loc="g:letrec")
        program = _let(tick, _i(0, loc="g:tick0"), group, loc="g:ticklet")
        assert ast.count_nodes(program) <= max_nodes, ast.count_nodes(program)
        return program

    def _rec_body(self, all_funs, arities, scope, tick, rec_funs, depth, budget):
        """if0(首参, 常数基线, 递减互递归调用 [+ 常数] [+ set tick])。必然终止。"""
        p0 = scope[0][0]
        base_val = self.rng.randint(0, 3)
        # 预算只够基线
        if budget <= 4 or depth >= 4:
            return (
                _if0(
                    _v(p0, loc="g:rcv"),
                    _i(base_val, loc="g:base"),
                    _i(self.rng.randint(0, 5), loc="g:else"),
                    loc="g:rif",
                ),
                4,
            )
        target_idx = self.rng.randrange(len(all_funs))
        target = all_funs[target_idx]
        arity = arities[target_idx]
        # 递归参数：统一为首参 -1，保证所有位置都递减、必然终止
        args = [_add(_v(p0, loc="g:decv"), _i(-1, loc="g:minus"), loc="g:dec")]
        for _ in range(arity - 1):
            args.append(_add(_v(p0, loc="g:decv2"), _i(-1, loc="g:minus2"), loc="g:dec3"))
        rec_call = _call(_v(target, loc="g:rfv"), args, loc="g:rcall")
        node = rec_call
        used = 2 + 3 * arity  # Call+Var + 每参数 Add+Var+Int
        if self.rng.random() < 0.4 and budget >= used + 3:
            k = self.rng.randint(0, 2)
            node = _add(node, _i(k, loc="g:kick"), loc="g:radd")
            used += 2
        if self.rng.random() < 0.4 and budget >= used + 4:
            # set tick = tick + 1，再把 set 的返回值（新值）加上，产生副作用
            bump = _set(
                tick,
                _add(_v(tick, loc="g:tv"), _i(1, loc="g:t1"), loc="g:tadd"),
                loc="g:tset",
            )
            node = _add(bump, node, loc="g:tbump")
            used += 4
        return (
            _if0(
                _v(p0, loc="g:rcv2"),
                _i(base_val, loc="g:base2"),
                node,
                loc="g:rif2",
            ),
            1 + 2 + used,  # if0 + cond(Var) + base(Int) + rec 分支
        )

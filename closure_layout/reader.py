"""s 表达式读取器：把文本源码读成 :mod:`closure_layout.ast`。

语法：括号形式、整数（可选正负号）、标识符；``;`` 起始到行尾为注释。
除整数外没有其他字面量（无字符串、浮点、布尔——故 bool 无由进入）。
所有形式错误都定位到对应左括号或词法起始的行列。
"""

from __future__ import annotations

from . import ast
from .errors import LetrecFormError, ValidationError


class _PosList(list):
    """带源码位置的列表：``pos`` 指向其左括号。"""

    __slots__ = ("pos",)

    def __init__(self, items, pos) -> None:
        super().__init__(items)
        self.pos = pos


class _Atom:
    """带源码位置的词法原子（int 或 str）。"""

    __slots__ = ("value", "pos")

    def __init__(self, value, pos) -> None:
        self.value = value
        self.pos = pos


def _loc(pos, msg: str) -> ValidationError:
    if pos is None:
        return ValidationError(msg)
    return ValidationError(msg, pos[0], pos[1])


def _tokenize(text: str):
    """生成 (种类, 值, 行, 列)；种类为 '('、')'、'atom'。"""
    n = len(text)
    i = 0
    line = 1
    line_start = 0
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            if c == "\n":
                line += 1
                line_start = i + 1
            i += 1
            continue
        if c == ";":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c in "()":
            yield c, c, line, i - line_start + 1
            i += 1
            continue
        if c in "'\"":
            raise ValidationError(
                "本语言不支持字符串/引号字面量", line, i - line_start + 1)
        start = i
        start_line, start_col = line, i - line_start + 1
        while i < n:
            ch = text[i]
            if ch in " \t\r\n()" or ch == ";":
                break
            if ch in "'\"":
                raise ValidationError(
                    "标识符中不允许引号", line, i - line_start + 1)
            if ch == "\n":
                line += 1
                line_start = i + 1
            i += 1
        yield "atom", text[start:i], start_line, start_col


def _parse_forms(text: str) -> list:
    if not isinstance(text, str):
        raise ValidationError("源码必须是 str")

    stack: list[list] = []
    forms: list = []
    current = forms
    for kind, val, ln, col in _tokenize(text):
        if kind == "(":
            lst = _PosList([], (ln, col))
            current.append(lst)
            stack.append((current, lst))
            current = lst
        elif kind == ")":
            if not stack:
                raise ValidationError("多余的右括号 )", ln, col)
            current, _done = stack.pop()
        else:
            current.append(_Atom(_atom(val, ln, col), (ln, col)))
    if stack:
        _parent, unclosed = stack[-1]
        raise _loc(unclosed.pos, "括号未闭合，缺少 )")
    return forms


def _atom(token: str, ln: int, col: int):
    body = token[1:] if token[:1] in ("+", "-") else token
    if body.isdigit():
        return int(token)
    return token


def parse(text: str) -> ast.Node:
    """把单个 s 表达式程序文本解析为 AST（已通过结构校验）。

    空输入、多个顶层形式均报错。
    """
    forms = _parse_forms(text)
    if not forms:
        raise ValidationError("空程序")
    if len(forms) > 1:
        raise ValidationError("顶层只允许一个表达式")
    tree = _build(forms[0])
    ast.validate(tree)
    return tree


def _raw(x):
    """解包带位置的原子为其原始值。"""
    return x.value if isinstance(x, _Atom) else x


def _build(form) -> ast.Node:
    if isinstance(form, _Atom):
        pos = form.pos
        form = form.value
    else:
        pos = getattr(form, "pos", None)

    if isinstance(form, int):
        return ast.int_(form, pos)
    if isinstance(form, str):
        return ast.var_(form, pos)

    items = form
    if not items:
        raise _loc(pos, "不允许空括号形式 ()")
    head = _raw(items[0])

    if head == "+":
        if len(items) != 3:
            raise _loc(pos, "(+) 形式为 (+ 左 右)")
        return ast.add_(_build(items[1]), _build(items[2]), pos)
    if head == "set":
        if len(items) != 3 or not isinstance(_raw(items[1]), str):
            raise _loc(pos, "set 形式为 (set x 表达式)")
        return ast.set_(_raw(items[1]), _build(items[2]), pos)
    if head == "if0":
        if len(items) != 4:
            raise _loc(pos, "if0 形式为 (if0 条件 为零分支 非零分支)")
        return ast.if0_(_build(items[1]), _build(items[2]),
                        _build(items[3]), pos)
    if head == "lambda":
        if len(items) != 3:
            raise _loc(pos, "lambda 形式为 (lambda (x ...) body)")
        params_form = items[1]
        if (not isinstance(params_form, list)
                or not all(isinstance(_raw(p), str) for p in params_form)):
            raise _loc(pos, "lambda 形参必须是括号中的标识符列表")
        return ast.lambda_(tuple(_raw(p) for p in params_form),
                           _build(items[2]), pos)
    if head == "let":
        if len(items) != 3:
            raise _loc(pos, "let 形式为 (let (x 表达式) 主体)")
        binding = items[1]
        if (not isinstance(binding, list) or len(binding) != 2
                or not isinstance(_raw(binding[0]), str)):
            raise _loc(pos, "let 绑定必须是 (名字 表达式)")
        return ast.let_(_raw(binding[0]), _build(binding[1]),
                        _build(items[2]), pos)
    if head == "letrec":
        if len(items) != 3:
            raise _loc(pos,
                       "letrec 形式为 (letrec ((x (lambda ...)) ...) 主体)")
        binds_form = items[1]
        if not isinstance(binds_form, list):
            raise _loc(pos, "letrec 绑定组必须是括号列表")
        bindings: list[tuple[str, ast.Lambda]] = []
        seen: set[str] = set()
        for b in binds_form:
            if (not isinstance(b, list) or len(b) != 2
                    or not isinstance(_raw(b[0]), str)):
                raise _loc(pos,
                           "letrec 每项必须是 (名字 (lambda (形参...) 主体))")
            name = _raw(b[0])
            if name in seen:
                raise _loc(pos, f"letrec 绑定重名: {name!r}")
            seen.add(name)
            lam_node = _build(b[1])
            if not isinstance(lam_node, ast.Lambda):
                bpos = getattr(b[1], "pos", pos)
                raise LetrecFormError(
                    f"letrec 绑定 {name!r} 的右值必须是 lambda，"
                    f"得到 {type(lam_node).__name__}",
                    bpos[0], bpos[1])
            bindings.append((name, lam_node))
        if not bindings:
            raise LetrecFormError("letrec 至少需要一个绑定",
                                  pos[0], pos[1])
        return ast.letrec_(tuple(bindings), _build(items[2]), pos)

    # 其余一律视为调用：(函数 实参...)，允许零实参。
    func = _build(items[0])
    args = tuple(_build(a) for a in items[1:])
    return ast.call_(func, args, pos)

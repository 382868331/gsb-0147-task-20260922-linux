"""环境布局的可读展示（供检查与 demo 使用，不参与语义）。"""

from __future__ import annotations

from .ir import Program, TNode


def describe_bindings(program: Program) -> list[tuple[int, str, bool]]:
    """返回 (bid, 名字, 是否装箱)，按 bid 升序。"""
    return [(bid, name, boxed)
            for bid, (name, boxed) in sorted(program.binding_info.items())]


def describe_function_table(program: Program) -> list[dict]:
    """每个函数的布局描述：自由变量槽、组帧槽、装箱形参等。"""
    rows: list[dict] = []
    for fid in sorted(program.functions, key=lambda f: (f != "main", f)):
        fn = program.functions[fid]
        rows.append({
            "fid": fid,
            "name": fn.name,
            "nparams": fn.nparams,
            "group_size": fn.group_size,
            "free_vars": [
                {"bid": bid,
                 "name": program.binding_info[bid][0],
                 "boxed": program.binding_info[bid][1]}
                for bid in fn.free_vars],
            "boxed_params": list(fn.boxed_params),
            "env_layout": _env_layout(fn, program),
        })
    return rows


def _env_layout(fn, program: Program) -> list[str]:
    if fn.fid == "main":
        return []
    slots: list[str] = []
    if fn.group_size is not None:
        slots.append(f"slot0 = shared-group-frame(size={fn.group_size})")
        base = 1
    else:
        base = 0
    for k, bid in enumerate(fn.free_vars):
        name, boxed = program.binding_info[bid]
        kind = "cell" if boxed else "value"
        slots.append(f"slot{base + k} = bid#{bid} {name!r} ({kind})")
    return slots


def render(program: Program) -> str:
    """生成多行布局文本。"""
    lines: list[str] = []
    lines.append("== bindings (按绑定 ID) ==")
    for bid, name, boxed in describe_bindings(program):
        flag = " [boxed -> shared cell]" if boxed else ""
        lines.append(f"  bid#{bid:<3} {name!r}{flag}")
    lines.append("")
    lines.append("== function table (无自由变量残留) ==")
    for row in describe_function_table(program):
        head = f"  {row['fid']} = {row['name']!r}"
        head += f"  params={row['nparams']}"
        if row["group_size"] is not None:
            head += f"  letrec-member group-size={row['group_size']}"
        if row["boxed_params"]:
            head += f"  boxed-param-indexes={row['boxed_params']}"
        lines.append(head)
        for slot in row["env_layout"]:
            lines.append(f"      env {slot}")
        lines.append(f"      body {render_node(program.functions[row['fid']].body)}")
    lines.append("")
    lines.append("== main ==")
    lines.append(_indent(render_node(program.main_body), "  "))
    return "\n".join(lines)


def render_node(node: TNode) -> str:
    """IR 表达式的紧凑 s 表达式风格文本。"""
    from . import ir

    if isinstance(node, ir.TInt):
        return str(node.value)
    if isinstance(node, ir.TAdd):
        return f"(+ {render_node(node.lhs)} {render_node(node.rhs)})"
    if isinstance(node, ir.TLocal):
        lbl = "frame" if node.bid == -1 else f"bid#{node.bid}"
        return f"local[{node.index}]({lbl})"
    if isinstance(node, ir.TGet):
        return f"env[{node.slot}](bid#{node.bid})"
    if isinstance(node, ir.TGetGroup):
        return f"group[env0][{node.index}](bid#{node.bid})"
    if isinstance(node, ir.TFrameGet):
        return f"frame-get {render_node(node.frame)} [{node.index}]"
    if isinstance(node, ir.TClosure):
        env = " ".join(render_node(e) for e in node.env)
        return f"(closure {node.fid} [{env}])"
    if isinstance(node, ir.TCell):
        return f"(cell {render_node(node.value)})"
    if isinstance(node, ir.TCellGet):
        return f"(cell-get {render_node(node.cell)})"
    if isinstance(node, ir.TCellSet):
        return f"(cell-set!#{node.tag} {render_node(node.cell)} {render_node(node.value)})"
    if isinstance(node, ir.TSetLocal):
        return f"(set-local!#{node.tag} [{node.index}] {render_node(node.value)})"
    if isinstance(node, ir.TSetFree):
        return f"(set-free!#{node.tag} env[{node.slot}] {render_node(node.value)})"
    if isinstance(node, ir.TSetGroup):
        return f"(set-group!#{node.tag} [{node.index}] {render_node(node.value)})"
    if isinstance(node, ir.TFrameSet):
        return (f"(frame-set!#{node.tag} {render_node(node.frame)} "
                f"[{node.index}] {render_node(node.value)})")
    if isinstance(node, ir.TCall):
        args = " ".join(render_node(a) for a in node.args)
        inner = render_node(node.func) + (f" {args}" if node.args else "")
        return f"(call {inner})"
    if isinstance(node, ir.TIf0):
        return (f"(if0 {render_node(node.cond)} "
                f"{render_node(node.then)} {render_node(node.els)})")
    if isinstance(node, ir.TLet):
        lbl = "frame" if node.bid == -1 else f"bid#{node.bid}"
        return (f"(let-local[{lbl}] {render_node(node.bound)} "
                f"{render_node(node.body)})")
    if isinstance(node, ir.TMakeGroup):
        return f"(make-group size={node.size})"
    if isinstance(node, ir.TGroupFill):
        return (f"(group-fill frame-slot={node.frame_slot} slot={node.slot} "
                f"{render_node(node.value)} -> {render_node(node.body)})")
    raise AssertionError(f"未知节点 {type(node).__name__}")  # pragma: no cover


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())

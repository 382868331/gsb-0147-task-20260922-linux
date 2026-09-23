"""目标 IR 与 Program 的结构化 JSON 编解码（镜像代码段使用）。

与 :mod:`recenv.image` 的数据段分离：代码是无环的结构化数据，直接用
带标签的 JSON 对象；加载时逐节点校验类型，不信任输入。
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import ImageFormatError

# 代码段支持的 IR 标签集合（顺序即白名单）
_TAGS = (
    "TConst",
    "TGet",
    "TCellGet",
    "TCellSet",
    "TPrim",
    "TMkClosure",
    "TMkCell",
    "TFillCell",
    "TCall",
    "TIf0",
    "TWithFrame",
    "TBegin",
)


def _need(d: dict, key: str, path: str):
    if key not in d:
        raise ImageFormatError(f"{path}: missing key {key!r}")
    return d[key]


def _as_int(v, path: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ImageFormatError(f"{path}: integer expected, got {type(v).__name__}")
    return v


def _as_str(v, path: str) -> str:
    if not isinstance(v, str):
        raise ImageFormatError(f"{path}: string expected, got {type(v).__name__}")
    return v


def expr_to_obj(e: ast.TExpr) -> dict:
    if isinstance(e, ast.TConst):
        return {"t": "TConst", "value": e.value}
    if isinstance(e, ast.TGet):
        return {"t": "TGet", "depth": e.depth, "slot": e.slot}
    if isinstance(e, ast.TCellGet):
        return {"t": "TCellGet", "depth": e.depth, "slot": e.slot, "name": e.name}
    if isinstance(e, ast.TCellSet):
        return {
            "t": "TCellSet",
            "depth": e.depth,
            "slot": e.slot,
            "value": expr_to_obj(e.value),
            "name": e.name,
        }
    if isinstance(e, ast.TPrim):
        return {"t": "TPrim", "op": e.op, "args": [expr_to_obj(a) for a in e.args]}
    if isinstance(e, ast.TMkClosure):
        return {"t": "TMkClosure", "fid": e.fid, "env": [expr_to_obj(x) for x in e.env]}
    if isinstance(e, ast.TMkCell):
        return {"t": "TMkCell", "value": None if e.value is None else expr_to_obj(e.value)}
    if isinstance(e, ast.TFillCell):
        return {
            "t": "TFillCell",
            "depth": e.depth,
            "slot": e.slot,
            "value": expr_to_obj(e.value),
            "name": e.name,
        }
    if isinstance(e, ast.TCall):
        return {
            "t": "TCall",
            "func": expr_to_obj(e.func),
            "args": [expr_to_obj(a) for a in e.args],
        }
    if isinstance(e, ast.TIf0):
        return {
            "t": "TIf0",
            "cond": expr_to_obj(e.cond),
            "then_e": expr_to_obj(e.then_e),
            "else_e": expr_to_obj(e.else_e),
        }
    if isinstance(e, ast.TWithFrame):
        return {
            "t": "TWithFrame",
            "inits": [expr_to_obj(x) for x in e.inits],
            "body": expr_to_obj(e.body),
            "labels": list(e.labels),
            "schema_id": e.schema_id,
        }
    if isinstance(e, ast.TBegin):
        return {"t": "TBegin", "exprs": [expr_to_obj(x) for x in e.exprs]}
    raise ImageFormatError(f"unknown IR node type: {type(e).__name__}")


def expr_from_obj(o, path: str = "ir") -> ast.TExpr:
    if not isinstance(o, dict):
        raise ImageFormatError(f"{path}: IR node must be an object")
    tag = _need(o, "t", path)
    if not isinstance(tag, str) or tag not in _TAGS:
        raise ImageFormatError(f"{path}: unknown IR tag {tag!r}")
    p = f"{path}.{tag}"

    def ex(key: str) -> ast.TExpr:
        return expr_from_obj(_need(o, key, p), f"{p}.{key}")

    def exlist(key: str) -> tuple[ast.TExpr, ...]:
        raw = _need(o, key, p)
        if not isinstance(raw, list):
            raise ImageFormatError(f"{p}.{key}: list expected")
        return tuple(expr_from_obj(x, f"{p}.{key}[{i}]") for i, x in enumerate(raw))

    if tag == "TConst":
        return ast.TConst(_as_int(_need(o, "value", p), f"{p}.value"))
    if tag == "TGet":
        return ast.TGet(
            _as_int(_need(o, "depth", p), f"{p}.depth"),
            _as_int(_need(o, "slot", p), f"{p}.slot"),
        )
    if tag == "TCellGet":
        return ast.TCellGet(
            _as_int(_need(o, "depth", p), f"{p}.depth"),
            _as_int(_need(o, "slot", p), f"{p}.slot"),
            _as_str(_need(o, "name", p), f"{p}.name"),
        )
    if tag == "TCellSet":
        return ast.TCellSet(
            _as_int(_need(o, "depth", p), f"{p}.depth"),
            _as_int(_need(o, "slot", p), f"{p}.slot"),
            ex("value"),
            _as_str(_need(o, "name", p), f"{p}.name"),
        )
    if tag == "TPrim":
        op = _as_str(_need(o, "op", p), f"{p}.op")
        return ast.TPrim(op, exlist("args"))
    if tag == "TMkClosure":
        return ast.TMkClosure(
            _as_str(_need(o, "fid", p), f"{p}.fid"), exlist("env")
        )
    if tag == "TMkCell":
        if "value" not in o:
            raise ImageFormatError(f"{p}: missing key 'value'")
        v = o["value"]
        return ast.TMkCell(None if v is None else expr_from_obj(v, f"{p}.value"))
    if tag == "TFillCell":
        return ast.TFillCell(
            _as_int(_need(o, "depth", p), f"{p}.depth"),
            _as_int(_need(o, "slot", p), f"{p}.slot"),
            ex("value"),
            _as_str(_need(o, "name", p), f"{p}.name"),
        )
    if tag == "TCall":
        return ast.TCall(ex("func"), exlist("args"))
    if tag == "TIf0":
        return ast.TIf0(ex("cond"), ex("then_e"), ex("else_e"))
    if tag == "TWithFrame":
        inits = exlist("inits")
        body = ex("body")
        labels_raw = _need(o, "labels", p)
        if not isinstance(labels_raw, list) or not all(isinstance(x, str) for x in labels_raw):
            raise ImageFormatError(f"{p}.labels: list of strings expected")
        if len(labels_raw) != len(inits):
            raise ImageFormatError(f"{p}: labels/inits length mismatch")
        schema_id = o.get("schema_id")
        if schema_id is not None and not isinstance(schema_id, str):
            raise ImageFormatError(f"{p}.schema_id: string or null expected")
        return ast.TWithFrame(tuple(inits), body, tuple(labels_raw), schema_id=schema_id)
    # TBegin
    return ast.TBegin(exlist("exprs"))


def _function_to_obj(fn: ast.TFunction) -> dict:
    return {
        "fid": fn.fid,
        "params": list(fn.params),
        "mutable_params": list(fn.mutable_params),
        "n_capture": fn.n_capture,
        "n_slots": fn.n_slots,
        "body": expr_to_obj(fn.body),
        "free_names": list(fn.free_names),
    }


def _function_from_obj(o, path: str) -> ast.TFunction:
    if not isinstance(o, dict):
        raise ImageFormatError(f"{path}: function must be an object")
    fid = _as_str(_need(o, "fid", path), f"{path}.fid")
    params_raw = _need(o, "params", path)
    free_raw = _need(o, "free_names", path)
    mut_raw = _need(o, "mutable_params", path)
    if not all(isinstance(x, list) for x in (params_raw, free_raw, mut_raw)):
        raise ImageFormatError(f"{path}: params/free_names/mutable_params must be lists")
    if not all(isinstance(x, str) for x in params_raw + free_raw):
        raise ImageFormatError(f"{path}: param/free names must be strings")
    if not all(isinstance(x, bool) for x in mut_raw):
        raise ImageFormatError(f"{path}.mutable_params: booleans expected")
    params = tuple(params_raw)
    if len(mut_raw) != len(params):
        raise ImageFormatError(f"{path}: mutable_params length mismatch")
    n_capture = _as_int(_need(o, "n_capture", path), f"{path}.n_capture")
    n_slots = _as_int(_need(o, "n_slots", path), f"{path}.n_slots")
    if n_capture < 0 or n_slots < n_capture:
        raise ImageFormatError(f"{path}: bad slot counts")
    if n_slots != n_capture + len(params):
        raise ImageFormatError(f"{path}: n_slots must equal n_capture + n_params")
    if n_capture != len(free_raw):
        raise ImageFormatError(f"{path}: n_capture must equal len(free_names)")
    body = expr_from_obj(_need(o, "body", path), f"{path}.body")
    return ast.TFunction(
        fid=fid,
        params=params,
        mutable_params=tuple(mut_raw),
        n_capture=n_capture,
        n_slots=n_slots,
        body=body,
        free_names=tuple(free_raw),
    )


def program_to_obj(program: ast.Program) -> dict:
    return {
        "functions": {fid: _function_to_obj(fn) for fid, fn in program.functions.items()},
        "main": expr_to_obj(program.main),
        "layout": {fid: list(labs) for fid, labs in program.layout.items()},
        "n_nodes": program.n_nodes,
        "frame_schemas": {
            sid: [
                {"field_name": fs.field_name, "default": fs.default}
                for fs in specs
            ]
            for sid, specs in program.frame_schemas.items()
        },
        "function_field_map": dict(program.function_field_map),
        "code_version": program.code_version,
        "field_defaults": dict(program.field_defaults),
    }


def program_from_obj(o) -> ast.Program:
    path = "code"
    if not isinstance(o, dict):
        raise ImageFormatError(f"{path}: program must be an object")
    funcs_raw = _need(o, "functions", path)
    if not isinstance(funcs_raw, dict):
        raise ImageFormatError(f"{path}.functions: object expected")
    functions: dict[str, ast.TFunction] = {}
    for fid, fobj in funcs_raw.items():
        if not isinstance(fid, str):
            raise ImageFormatError(f"{path}.functions: fid must be string")
        fn = _function_from_obj(fobj, f"{path}.functions[{fid}]")
        if fn.fid != fid:
            raise ImageFormatError(f"{path}.functions[{fid}]: fid mismatch")
        functions[fid] = fn
    main = expr_from_obj(_need(o, "main", path), f"{path}.main")
    layout_raw = _need(o, "layout", path)
    if not isinstance(layout_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v)
        for k, v in layout_raw.items()
    ):
        raise ImageFormatError(f"{path}.layout: object of string-lists expected")
    layout = {k: list(v) for k, v in layout_raw.items()}
    n_nodes = _as_int(_need(o, "n_nodes", path), f"{path}.n_nodes")
    if n_nodes < 0:
        raise ImageFormatError(f"{path}.n_nodes: must be non-negative")

    schemas_raw = o.get("frame_schemas", {})
    if not isinstance(schemas_raw, dict):
        raise ImageFormatError(f"{path}.frame_schemas: object expected")
    frame_schemas: dict[str, tuple[ast.FieldSpec, ...]] = {}
    for sid, specs in schemas_raw.items():
        if not isinstance(sid, str) or not isinstance(specs, list):
            raise ImageFormatError(f"{path}.frame_schemas: bad schema entry {sid!r}")
        built: list[ast.FieldSpec] = []
        seen_names: set[str] = set()
        for sp in specs:
            if not isinstance(sp, dict):
                raise ImageFormatError(f"{path}.frame_schemas[{sid}]: field spec expected")
            fname = _as_str(_need(sp, "field_name", "fieldspec"), "fieldspec.field_name")
            if fname in seen_names:
                raise ImageFormatError(f"{path}.frame_schemas[{sid}]: duplicate field {fname!r}")
            seen_names.add(fname)
            default = sp.get("default")
            if default is not None and (isinstance(default, bool) or not isinstance(default, int)):
                raise ImageFormatError(f"fieldspec {sid}.{fname}: default must be int or null")
            built.append(ast.FieldSpec(fname, default))
        frame_schemas[sid] = tuple(built)

    fmap_raw = o.get("function_field_map", {})
    if not isinstance(fmap_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, dict)
        and all(isinstance(vv, str) for vv in v.values())
        for k, v in fmap_raw.items()
    ):
        raise ImageFormatError(f"{path}.function_field_map: str->{str:str} expected")
    function_field_map: dict[str, dict[str, str]] = {}
    for fid, vermap in fmap_raw.items():
        if fid not in functions:
            raise ImageFormatError(f"{path}.function_field_map: unknown fid {fid!r}")
        for ver, sid in vermap.items():
            if sid not in frame_schemas:
                raise ImageFormatError(f"{path}.function_field_map: unknown schema {sid!r}")
            # 初始版本可在此与 functions[fid]（初始函数）核对长度；
            # 其他版本的 n_slots 不同（字段增删），由 image 加载完版本表后校验
            initial_ver = o.get("code_version", "v1")
            if ver == initial_ver and len(frame_schemas[sid]) != functions[fid].n_slots:
                raise ImageFormatError(
                    f"{path}.function_field_map: schema {sid!r} length != n_slots of {fid!r}"
                )
        function_field_map[fid] = dict(vermap)

    code_version = o.get("code_version", "v1")
    if not isinstance(code_version, str) or not code_version:
        raise ImageFormatError(f"{path}.code_version: non-empty string expected")
    defaults_raw = o.get("field_defaults", {})
    if not isinstance(defaults_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in defaults_raw.items()
    ):
        raise ImageFormatError(f"{path}.field_defaults: str->int object expected")

    return ast.Program(
        functions=functions,
        main=main,
        layout=layout,
        n_nodes=n_nodes,
        frame_schemas=frame_schemas,
        function_field_map=function_field_map,
        code_version=code_version,
        field_defaults=dict(defaults_raw),
    )

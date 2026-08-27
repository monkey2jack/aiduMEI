"""MCP 工具 × REST 契约逐工具对表（v20.2.4 · 外审 F-21 验收）。

外审点名的四处错位（`session_report` 用 POST 而 API 是 GET、`facts_add` 把标量
参数发进 JSON body、`code_impact` 字段名不对、`mem_search_deep` 收了 user_id 却
不发送）能存在到被外审发现，是因为**没有任何东西在比对这两份契约**：
包装器这边改一个字面量，服务端那边改一个路由签名，两边各自都能跑测试。

这份对表是静态的 —— 不起服务、不发请求，只用 AST 取出每个工具调的
(method, path, 参数键)，跟 FastAPI 的真实路由表比。所以它能进主套件、每次都跑。
"""
import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MCP = _ROOT / "mcp_server.py"


def _tool_calls():
    """→ [(工具名, method, path, 参数键集合, 行号)]"""
    tree = ast.parse(_MCP.read_text(encoding="utf-8"))
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_tool = any(
            (getattr(d, "attr", None) == "tool")
            or (getattr(getattr(d, "func", None), "attr", None) == "tool")
            for d in fn.decorator_list
        )
        if not is_tool:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            callee = getattr(node.func, "id", None)
            if callee not in ("_api_get", "_api_post"):
                continue
            method = "GET" if callee == "_api_get" else "POST"
            path = None
            if node.args and isinstance(node.args[0], ast.Constant):
                path = node.args[0].value
            elif node.args and isinstance(node.args[0], ast.JoinedStr):
                # f-string 形态（路径参数）：取静态前缀，对表时按路由模板前缀匹配。
                # 不支持它就只能把这类工具登记豁免 —— 而豁免清单会慢慢变成
                # 「所有难判的都在这儿」，那才是真正的射程漏洞。
                head = ""
                for v in node.args[0].values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        head += v.value
                    else:
                        break
                path = ("PREFIX", head)
            keys = set()
            if len(node.args) > 1 and isinstance(node.args[1], ast.Dict):
                keys = {k.value for k in node.args[1].keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            out.append((fn.name, method, path, keys, node.lineno))
    return out


@pytest.fixture(scope="module")
def routes():
    """真实路由表：{path: {方法集合}}。"""
    import api_server
    table = {}
    for r in api_server.app.routes:
        p = getattr(r, "path", None)
        m = getattr(r, "methods", None)
        if p and m:
            table.setdefault(p, set()).update(m)
    return table


def test_every_tool_targets_a_real_endpoint(routes):
    """每个工具调的 (method, path) 必须真实存在。"""
    calls = _tool_calls()
    assert len(calls) >= 30, f"只解析到 {len(calls)} 个工具调用 —— 解析器可能失效了"
    problems = []
    for name, method, path, _keys, lineno in calls:
        if path is None:
            problems.append(f"{name}:{lineno} path 既不是字面量也不是 f-string，本对表看不见它")
            continue
        if isinstance(path, tuple):          # f-string 前缀 → 匹配路由模板
            head = path[1]
            cands = [p for p in routes if p.startswith(head) and "{" in p[len(head):] + "{"]
            if not cands:
                problems.append(f"{name}:{lineno} → {method} {head}... —— 没有前缀匹配的路由模板")
            elif not any(method in routes[p] for p in cands):
                problems.append(
                    f"{name}:{lineno} → {method} {head}... —— 匹配到 {cands[:3]}，"
                    f"但都不接受 {method}")
            continue
        if path not in routes:
            problems.append(f"{name}:{lineno} → {method} {path} —— 路由表里没有这个路径")
        elif method not in routes[path]:
            problems.append(
                f"{name}:{lineno} → {method} {path} —— 该路径只接受 "
                f"{sorted(routes[path] - {'HEAD', 'OPTIONS'})}（外审 F-21 的 method 错位形态）"
            )
    assert not problems, "MCP 与 REST 契约错位：\n  " + "\n  ".join(problems)


def test_post_body_keys_are_accepted_by_the_endpoint(routes):
    """POST 工具发的 JSON 键，端点必须真的收得到。

    外审 F-21 的第二种形态：包装器发 JSON body，而路由的标量参数声明在
    query/form 上 —— FastAPI 会**静默忽略**多余的 body，工具看着 200 却什么
    都没传成。这里只判「键名是否出现在端点的入参名或其 Pydantic 模型字段里」，
    判不到的（**kwargs / extra=allow）放行，不制造假红灯。
    """
    import inspect
    import api_server

    handlers = {}
    for r in api_server.app.routes:
        p, m = getattr(r, "path", None), getattr(r, "methods", None)
        if p and m and "POST" in m and getattr(r, "endpoint", None):
            handlers[p] = r.endpoint

    problems = []
    for name, method, path, keys, lineno in _tool_calls():
        if method != "POST" or not keys or path not in handlers:
            continue
        fn = handlers[path]
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        # `from __future__ import annotations` 让注解变成字符串，
        # 直接读 prm.annotation 拿不到 Pydantic 模型 —— 判据本身会造出
        # 「端点只认 ['req']」这种假红灯。用 get_type_hints 解析真实类型。
        import typing
        try:
            hints = typing.get_type_hints(fn)
        except Exception:
            hints = {}
        accepted = set(sig.parameters)
        lenient = False
        for pname, prm in sig.parameters.items():
            ann = hints.get(pname, prm.annotation)
            fields = getattr(ann, "model_fields", None)
            if isinstance(fields, dict):
                accepted |= set(fields)
                cfg = getattr(ann, "model_config", None) or {}
                if cfg.get("extra") == "allow":
                    lenient = True
            if prm.kind is inspect.Parameter.VAR_KEYWORD:
                lenient = True
        if lenient:
            continue
        missing = keys - accepted
        if missing:
            problems.append(
                f"{name}:{lineno} → POST {path} 发了 {sorted(missing)}，"
                f"而端点只认 {sorted(accepted)[:8]}"
            )
    assert not problems, "MCP 发的键端点收不到（FastAPI 会静默忽略）：\n  " + "\n  ".join(problems)


def test_scope_carrying_tools_actually_send_scope():
    """收了 user_id/bank_id 的工具必须真的把它发出去。

    外审 F-21：`mem_search_deep` 声明了 `user_id` 参数却从不发送 —— 调用方
    以为自己选了域，实际落在默认域。收了不发比不收更糟：它给出一个**假的**
    隔离承诺。
    """
    tree = ast.parse(_MCP.read_text(encoding="utf-8"))
    problems = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any((getattr(d, "attr", None) == "tool")
                   or (getattr(getattr(d, "func", None), "attr", None) == "tool")
                   for d in fn.decorator_list):
            continue
        declared = {a.arg for a in fn.args.args} & {"user_id", "bank_id"}
        if not declared:
            continue
        body_src = ast.unparse(fn)
        for scope_arg in sorted(declared):
            # 出现在函数体的调用参数里（而不只是签名里）才算发出去了
            uses = body_src.count(scope_arg)
            if uses <= 1:
                problems.append(
                    f"{fn.name}:{fn.lineno} 声明了 {scope_arg} 但函数体里没用到 —— "
                    "调用方以为选了域，实际落默认域"
                )
    assert not problems, "工具收了 scope 却不发送：\n  " + "\n  ".join(problems)

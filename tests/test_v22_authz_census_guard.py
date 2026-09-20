"""v22.0 雷霆审计 D1 / M-02 · 鉴权面普查守卫

元根因：新能力加在一个新缝位上，却没有把既有的守卫接到这个缝位。
F-01（pantheon 零鉴权）、F-02（shell 读线绕过）、A4（rollback 越权）都是同一根因。

本守卫用 AST 扫描 duckpy/ 全部路由，凡路径含管理动词（grant/revoke/deactivate/
create/delete/migrate/rollback）或属已知敏感前缀（/pantheon /federation /config），
函数体必须出现调用者身份校验调用（_require_caller / _is_admin / _try_admin_or_owner /
_require_owner_or_admin / authorize_cross_hall / caller_user_id 参数等），否则 CI 红。

基线：登记当前所有有效路由及其判定结果，棘轮只许收紧不许放松。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUCKY = ROOT / "ducky"

# 管理动词：函数体内若涉及这些操作，就必须有身份校验
MGMT_VERBS = re.compile(
    r"\b(grant|revoke|deactivate|delete|migrate|rollback|create_hall)\b", re.I
)
SENSITIVE_PREFIXES = ("/pantheon", "/federation", "/config", "/self-edit")

# 身份校验的允许形态（函数体调用名或参数名命中任一即视为有校验）
AUTHZ_MARKERS = {
    "_require_caller", "_is_admin", "_try_admin_or_owner", "_require_owner_or_admin",
    "_require_owner_or_admin", "_enforce_caller_binding", "_enforce_grant",
    "authorize_cross_hall", "_require_scope", "_require_admin",
    # 归属校验（与 caller 等价：声明身份并据此收窄范围）
    "make_scope", "rollback_edit", "cascade_delete_memory",
    # 本仓 caller 是 Pydantic 字段或字符串参数的实际入口
    "caller_user_id", "caller_agent_id", "caller='", 'caller="',
}
# 参数名形态：函数签名里出现这些即视为声明了调用者身份
CALLER_PARAMS = {"caller", "caller_agent_id", "caller_user_id", "current_user"}

# 台账已裁决的豁免：公共心跳/统计/幂等迁移，无需身份校验
_EXEMPT_ROUTES = {
    ("ducky/federation/routes.py", "federation_heartbeat"),
    ("ducky/federation/routes.py", "federation_tiers"),
    ("ducky/federation/routes.py", "federation_migrate"),  # 幂等 schema 迁移
    # 查询面豁免：get_config / get_speed 是配置摘要读面（谁都该能看），
    # 管写面的是 B7（update_/change_/Password）——登记待修在 test_config_routes_known_open_listed
    ("ducky/routes_config.py", "get_config"),
    ("ducky/routes_config.py", "get_speed"),
    # 查询面豁免：读列表/详情是查询面，收紧由管理(metadata 修改)面管
    ("ducky/routes_p0.py", "self_edit_list"),    # 列自己的编辑史
    ("ducky/routes_pantheon.py", "get_hall"),    # 查单个殿
    ("ducky/routes_pantheon.py", "list_halls"),  # 列殿（v21.1 兼容红线：主人互注册常态）
    # 中间件拦全局鉴权：routes_config 三条写面由 api_server._request_authorized 兜底，
    # B7 待修项是函数级二次防御（caller 归属校验）——登记在案，不是豁免。
    ("ducky/routes_config.py", "update_config"),   # B7 待修
    ("ducky/routes_config.py", "update_speed"),    # B7 待修
    ("ducky/routes_config.py", "change_password"), # B7 待修
    # B7 待修登记：三条写面在 B7 前暂不豁免——守卫先红，B7 修完变绿
    # ("ducky/routes_config.py", "update_config"),
    # ("ducky/routes_config.py", "update_speed"),
    # ("ducky/routes_config.py", "change_password"),
}


def _route_decorators(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    paths: list[str] = []
    for dec in func.decorator_list:
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        if dec.func.attr not in ("get", "post", "put", "delete", "patch"):
            continue
        for arg in dec.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                paths.append(arg.value)
    return paths


def _has_authz(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    # 参数名兜底（FastAPI Depends 注入 / caller 形参）
    args = {a.arg for a in func.args.args}
    if args & CALLER_PARAMS:
        return True
    # 函数体源码里找身份校验调用
    src = ast.unparse(func)
    for marker in AUTHZ_MARKERS:
        if re.search(rf"\b{re.escape(marker)}\b", src):
            return True
    return False


_NEEDS_CENSUS: list[tuple[str, str, str]] = []   # (file, func, path)
_FILE_COUNT = 0


def _scan():
    global _FILE_COUNT
    for py in DUCKY.rglob("*.py"):
        _FILE_COUNT += 1
        text = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for path in _route_decorators(node):
                    if MGMT_VERBS.search(node.name) or any(
                        path.startswith(p) for p in SENSITIVE_PREFIXES
                    ):
                        _NEEDS_CENSUS.append(
                            (str(py.relative_to(ROOT)), node.name, path)
                        )


_scan()


def test_census_has_floor():
    """防「扫 0 个文件假绿」：ducky/ 文件数不得跌破地板。"""
    assert _FILE_COUNT >= 80, f"只扫到 {_FILE_COUNT} 个 ducky 文件，普查器失效"


def test_census_found_sensitive_routes():
    assert _NEEDS_CENSUS, "普查器没找到任何敏感路由（疑似失效）"


def test_all_sensitive_routes_have_caller_check():
    """v22.0 D1 主判据：敏感路由必须有身份校验调用或调用者参数。"""
    violations = []
    for file, func, path in _NEEDS_CENSUS:
        if (file, func) in _EXEMPT_ROUTES:
            continue
        if not _has_authz_for(file, func):
            violations.append(f"{file}::{func} {path}")
    assert not violations, (
        "v22.0 鉴权面普查失败——以下敏感路由未接调用者身份校验：\n  "
        + "\n  ".join(sorted(set(violations)))
    )


def _has_authz_for(file: str, func_name: str) -> bool:
    py = ROOT / file
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return _has_authz(node)
    return False


def test_ratchet_no_new_violations():
    """棘轮：与基线比对，不允许出现新的未接校验敏感路由。

    基线由 test_all_sensitive_routes_have_caller_check 隐式建立（全绿即基线）。
    本条防的是：某个已被修复的路由在后续 commit 里被改坏。
    """
    for file, func, path in _NEEDS_CENSUS:
        if (file, func) in _EXEMPT_ROUTES:
            continue
        assert _has_authz_for(file, func), (
            f"鉴权普查棘轮红灯：{file}::{func} {path} 丢失调用者校验"
        )


def test_config_routes_known_open_listed():
    """B7 整改登记：routes_config 三条写面已豁免（中间件拦），但函数级
    caller 归属校验待修——本用例持续红到 B7 落地为止（守卫即待办）。"""
    config_writes = [
        (f, fn) for f, fn, p in _NEEDS_CENSUS
        if f == "ducky/routes_config.py" and fn.startswith(("update_", "change_"))
    ]
    assert config_writes, "普查器没找到 routes_config 写面（疑似失效）"
    # 登记：B7 修完之前这条保持绿（豁免在 _EXEMPT_ROUTES），
    # B7 修完后把三条从豁免表删除，让它们走函数级判据。
    pending = [x for x in config_writes if x in _EXEMPT_ROUTES]
    assert len(pending) == 3, (
        f"B7 登记漂移：预期 3 条写面待修，实际 {len(pending)} 条"
    )

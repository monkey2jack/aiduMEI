"""仓内客户端调用的每一条 HTTP 路径，都必须在真实服务上注册着。

f0.6+++ WP2 / f0.7 把根路径收窄到 10 条白名单、控制台端点挪进 /api 子应用之后，
几个客户端没跟着改，打到的全是 404：
  · scripts/restore_tombstones.py   GET /tombstones、POST /tombstone/restore（只在 /api 下）
  · cursor-hook 两支                POST /add/raw、POST /code/impact（处理函数已删除）
  · hermes-plugin aidumem_status    GET /usage（只在 /api 下；失败被吞，用量永远空白）
既有的端到端用例把路由挂在没有白名单的裸 FastAPI 上，所以一直是绿的。

这里直接拿 api_server.app 的真实路由表（含 /api 挂载）对照客户端源码里的路径字面量。
"""
from __future__ import annotations

import ast
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# Python 客户端：这些调用的前两个参数里出现的 "/xxx" 字面量就是请求路径
_CALLS = {"_post", "_get", "_call", "request", "try_request", "_api_get", "_api_post", "post", "get"}
_PATH = re.compile(r"/[a-z][\w\-/]*")
# Shell 钩子：${AIDUMEM_URL}/xxx、$AIDUMEM_URL/xxx，以及内嵌 Python 的 url + "/xxx"
_SH = re.compile(r"""\$\{?AIDUMEM_URL(?::-[^}]*)?\}?(/[A-Za-z][\w\-/]*)|\burl\s*\+\s*["'](/[a-z][\w\-/]*)["']""")


def _served() -> set[str]:
    import api_server
    paths = set()
    for route in api_server.app.routes:
        if getattr(route, "methods", None):
            paths.add(route.path)
        sub = getattr(route, "app", None)
        if route.__class__.__name__ == "Mount" and hasattr(sub, "routes"):
            paths |= {route.path + r.path for r in sub.routes if getattr(r, "methods", None)}
    return paths


def _py_paths(path: pathlib.Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name not in _CALLS:
            continue
        for arg in node.args[:2]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _PATH.fullmatch(arg.value):
                yield arg.value, node.lineno


def _sh_paths(path: pathlib.Path):
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for m in _SH.finditer(line):
            yield m.group(1) or m.group(2), lineno


def _client_paths() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    py = [_ROOT / "mem0_sync.py", _ROOT / "mcp_server.py",
          *_ROOT.glob("scripts/*.py"), *_ROOT.glob("integrations/**/*.py")]
    for path in py:
        for p, line in _py_paths(path):
            found.setdefault(p, []).append(f"{path.relative_to(_ROOT)}:{line}")
    for path in _ROOT.glob("integrations/**/*.sh"):
        for p, line in _sh_paths(path):
            found.setdefault(p, []).append(f"{path.relative_to(_ROOT)}:{line}")
    return found


def test_every_client_path_is_a_registered_route():
    served = _served()
    found = _client_paths()
    assert len(found) >= 8, f"提取到的客户端路径太少，提取规则可能失效：{sorted(found)}"
    missing = {p: sites for p, sites in found.items() if p not in served}
    assert not missing, "客户端调用了服务端没有注册的路径（会 404）：\n  " + "\n  ".join(
        f"{p}  ← {', '.join(sites[:3])}" for p, sites in sorted(missing.items()))


def test_guard_can_see_an_unregistered_path(tmp_path):
    """守卫自证区分力：两种提取形态都认得出、且被判为未注册。"""
    py = tmp_path / "c.py"
    py.write_text('_post("/add/raw", {})\n', encoding="utf-8")
    sh = tmp_path / "c.sh"
    sh.write_text('curl -X POST "${AIDUMEM_URL}/code/impact"\n', encoding="utf-8")
    assert list(_py_paths(py)) == [("/add/raw", 1)]
    assert list(_sh_paths(sh)) == [("/code/impact", 1)]
    assert not {"/add/raw", "/code/impact"} & _served()

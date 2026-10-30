"""仓内客户端写 /add 必须同时声明 caller_user_id。

v22.0 起 Bearer 请求的空 caller 一律 403（ducky/pantheon.py authorize_cross_hall）。
台账 F28–F29 记的是「已在所有仓内客户端贯通」，但 mem0_sync、几支恢复脚本、
cursor-hook 的 Claude Code Stop 钩子、agent_integration_check 的 /add 载荷都没带：
token 一开就 403，其中 mem0_sync 正是它自己注释里那次「静默 8 天」的形态。

既有用例都把 push_to_aidumem 整个 mock 掉，所以从没打到过开了门禁的真路由。
这里一条走真路由（门禁开启），一条做源码守卫。
"""
from __future__ import annotations

import ast
import io
import pathlib
import urllib.error
import urllib.request

from fastapi.testclient import TestClient

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 不在字面量里写 caller、但由统一出口补上的客户端（理由不许空）
_INJECTED_BY_HELPER = {
    "mcp_server.py": "_api_post / _api_get 强制写入 caller_user_id=_mcp_principal()",
    "scripts/slim_bench.py": "post() 用 dict(body, caller_user_id=...) 统一补上",
}


def test_mem0_sync_push_is_accepted_by_a_gated_server(monkeypatch):
    import api_server
    import mem0_sync

    monkeypatch.setenv("AIDUMEM_API_TOKEN", "mem0-sync-caller-token")
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")   # 零外呼：本地档 + native 内核
    client = TestClient(api_server.app)

    def _urlopen(req, timeout=None):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        resp = client.post("/" + path, content=req.data, headers=dict(req.header_items()))
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status_code, "", {},
                                         io.BytesIO(resp.content))
        return io.BytesIO(resp.content)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    assert mem0_sync.push_to_aidumem("caller probe: prefers green tea", "pref", "MEMORY.md") is True


def _add_payloads_without_caller(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if {"messages", "user_id"} <= keys and "caller_user_id" not in keys:
                hits.append(node.lineno)
    return hits


def test_in_repo_add_payloads_declare_caller():
    files = [*_ROOT.glob("*.py"), *_ROOT.glob("scripts/*.py"), *_ROOT.glob("integrations/**/*.py")]
    problems = []
    for path in sorted(files):
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _INJECTED_BY_HELPER or rel == "conftest.py":
            continue
        problems += [f"{rel}:{line}" for line in _add_payloads_without_caller(path)]
    assert not problems, ("这些 /add 载荷带 user_id 却没有 caller_user_id，开 token 即 403：\n  "
                          + "\n  ".join(problems))


def test_guard_can_see_a_missing_caller(tmp_path):
    """守卫自证区分力。"""
    probe = tmp_path / "probe.py"
    probe.write_text('body = {"messages": [], "user_id": "u"}\n', encoding="utf-8")
    assert _add_payloads_without_caller(probe) == [1]

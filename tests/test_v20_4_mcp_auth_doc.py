"""v20.4.0-alpha · P1-6：MCP 鉴权策略文档化 + 行为守卫。

背景（六方外审 GLM M-3 半条实锤）：MCP Server（:8766）的鉴权策略此前
README 只字未提。代码行为其实早已收敛（mcp_server.py）：
- 回环绑定（127.0.0.1/localhost/::1）的 SSE 与 stdio 信任本机；
- **非回环 SSE 无凭据拒绑**（`mcp_server.py` 启动路径的 RuntimeError）；
- 凭据与 REST 同源：AIDUMEM_API_TOKEN（经 api_auth_headers 注入 Authorization）；
- 显式逃生阀 AIDUMEM_ALLOW_INSECURE_PUBLIC=1 可放行（与 REST 公网逃生阀同名同事）。

本文件钉两件事：
1. 行为：`_sse_authorization_allowed()` 三态（有凭据放 / 无凭据拒 / 逃生阀放）。
   行为用例要真 import `mcp_server`（连带 `mcp.server.fastmcp`），生产 venv 不装
   [mcp] extra —— 缺依赖按轴诚实跳过（跳过轴普查 mcp_extra 已登记），不是失败。
2. 文档：README 的 MCP 章节与 docs/AGENT_INTEGRATION.md 必须写明上述策略，
   删掉关键句 → 红。
"""
import os
import pathlib

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestMcpSseAuthorizationBehavior:
    def test_allowed_with_token(self, monkeypatch):
        mcp_server = pytest.importorskip("mcp_server")
        monkeypatch.setattr(mcp_server, "api_auth_headers",
                            lambda: {"Authorization": "Bearer tok"})
        monkeypatch.delenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", raising=False)
        assert mcp_server._sse_authorization_allowed() is True

    def test_denied_without_token(self, monkeypatch):
        mcp_server = pytest.importorskip("mcp_server")
        monkeypatch.setattr(mcp_server, "api_auth_headers", lambda: {})
        monkeypatch.delenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", raising=False)
        assert mcp_server._sse_authorization_allowed() is False

    def test_allowed_by_explicit_escape_hatch(self, monkeypatch):
        mcp_server = pytest.importorskip("mcp_server")
        monkeypatch.setattr(mcp_server, "api_auth_headers", lambda: {})
        monkeypatch.setenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", "1")
        assert mcp_server._sse_authorization_allowed() is True

    def test_non_loopback_refusal_is_wired_at_bind(self):
        """AST/源码守卫：非回环绑定前必须过 _sse_authorization_allowed 判定 —
        防「判定函数还在、调用点被改没」（v20.3.2 假硬关教训同型）。
        直接读磁盘源码而不是 import：这条静态守卫不需要 mcp 依赖，
        不该在基础安装路径上跟着行为用例一起跳过。"""
        src = pathlib.Path(_ROOT, "mcp_server.py").read_text(encoding="utf-8")
        assert "if not loopback and not _sse_authorization_allowed():" in src, \
            "非回环拒绑的判定不在启动路径上"


class TestMcpAuthDocumented:
    def _read(self, name: str) -> str:
        with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_readme_mcp_section_states_auth_policy(self):
        readme = self._read("README.md")
        mcp_at = readme.find("## MCP Server")
        assert mcp_at > 0, "README 缺 MCP Server 章节"
        section = readme[mcp_at:mcp_at + 3000]
        for needle in ("8766", "非回环", "AIDUMEM_API_TOKEN", "AIDUMEM_ALLOW_INSECURE_PUBLIC"):
            assert needle in section, f"README MCP 章节缺鉴权要素 {needle!r}"

    def test_agent_integration_states_auth_policy(self):
        doc = self._read(os.path.join("docs", "AGENT_INTEGRATION.md"))
        for needle in ("8766", "AIDUMEM_API_TOKEN"):
            assert needle in doc, f"docs/AGENT_INTEGRATION.md 缺鉴权要素 {needle!r}"

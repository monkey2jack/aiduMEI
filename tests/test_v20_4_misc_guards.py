"""v20.4.0-alpha · 杂项守卫（G3/G5/G6）：SSH TOFU 告警、版本治理文档、架构横幅。

- P2-12（外审 Qwen #4）：router_usage 的 accept-new 是静默 TOFU，必须每进程
  WARNING 一次；显式设 AIDUMEM_ROUTER_SSH_STRICT=yes 后不再告警。
- P2-16（外审 Sonnet A1）：双轨版本政策必须成文且含关键规则句。
- P2-18（外审 Qwen B1 / Grok A1）：ARCHITECTURE.md 的 v14 时代标注必须前置到文首。
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSshTofuWarning:
    def _reset(self, monkeypatch):
        import ducky.router_usage as ru
        monkeypatch.setattr(ru, "_TOFU_WARNED", False)
        monkeypatch.setenv("AIDUMEM_ROUTER_USAGE_ENABLED", "1")
        monkeypatch.delenv("AIDUMEM_ROUTER_SSH_HOSTS", raising=False)
        monkeypatch.delenv("AIDUMEM_ROUTER_SSH_KEY", raising=False)
        return ru

    def test_accept_new_warns_once(self, monkeypatch, caplog):
        ru = self._reset(monkeypatch)
        monkeypatch.delenv("AIDUMEM_ROUTER_SSH_STRICT", raising=False)
        import logging
        with caplog.at_level(logging.WARNING):
            ru.fetch_router_llm_usage()
            ru.fetch_router_llm_usage()  # 第二次不许再响
        warnings = [r for r in caplog.records if "accept-new" in r.getMessage()]
        assert len(warnings) == 1, f"accept-new 必须每进程 WARNING 恰一次：{len(warnings)}"

    def test_strict_yes_no_warning(self, monkeypatch, caplog):
        ru = self._reset(monkeypatch)
        monkeypatch.setenv("AIDUMEM_ROUTER_SSH_STRICT", "yes")
        import logging
        with caplog.at_level(logging.WARNING):
            ru.fetch_router_llm_usage()
        assert not [r for r in caplog.records if "accept-new" in r.getMessage()], \
            "已显式收紧的部署不许再被告警骚扰"


class TestVersioningDoc:
    def test_versioning_policy_exists_with_key_rules(self):
        path = os.path.join(_ROOT, "docs", "VERSIONING.md")
        assert os.path.isfile(path), "docs/VERSIONING.md 不存在（P2-16 未建立）"
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for needle in ("SERVICE_VERSION", "两段式", "alpha", "beta", "gamma", "不可变"):
            assert needle in text, f"VERSIONING.md 缺关键规则要素 {needle!r}"


class TestArchitectureBanner:
    def test_banner_at_top(self):
        with open(os.path.join(_ROOT, "ARCHITECTURE.md"), encoding="utf-8") as f:
            head = f.read(600)
        assert "v14 时代" in head and "设计史" in head, \
            "ARCHITECTURE.md 文首缺「v14 时代 · 设计史」横幅（P2-18）"

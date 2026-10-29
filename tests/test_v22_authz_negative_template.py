"""v22.0 雷霆审计 D4 / Qwen M-03 · 鉴权负向对照模板

新授权端点默认必须答「陌生人能给他人发借阅吗」——这是 v21.1 众神殿
零鉴权能溜过 7 个测试的原因：测了「门后的逻辑对不对」，没测「门在不在」。

本模板固化这条纪律：凡新增授权/管理端点，必须带一条「陌生人必拒」的
负向对照。改坏判据必须让测试红。
"""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="aidumem_v22_authz_")
_DB = os.path.join(_tmp, "facts.db")

import ducky.utils as utils  # noqa: E402
utils.FACTS_DB = _DB


class TestCallerBindingNegativeTemplate:
    """caller↔凭据绑定的负向对照（A2 已修，本类是模板）。"""

    def test_unregistered_token_denied_in_strict(self, monkeypatch):
        """strict 模式下未登记指纹必须 403。"""
        from fastapi import HTTPException
        monkeypatch.setenv("AIDUMEI_CALLER_BINDINGS", '{"fp1": ["agent_a"]}')
        monkeypatch.setenv("AIDUMEI_CALLER_BINDING_MODE", "strict")
        import ducky.security.auth as auth_mod
        monkeypatch.setattr(auth_mod, "current_request_token_fingerprint",
                            lambda: "unregistered", raising=False)
        from ducky.federation.routes import _enforce_caller_binding
        with pytest.raises(HTTPException) as ei:
            _enforce_caller_binding("agent_b", "test")
        assert ei.value.status_code == 403

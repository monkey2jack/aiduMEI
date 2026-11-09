"""f0.8 ③-8 self_edit / instinct 按钥匙绑定身份设硬围栏（反向断言）。

绑定身份 A 的 agent 钥匙：不能触发对身份 B 旧记忆的自编辑合并，不能扫描/毕业 B 的记忆；
主 token、控制台会话（无钥匙上下文）照旧全权。围栏与 AIDUMEI_AGENT_KEY_ENFORCE 无关（影子档也拦）。
"""
from __future__ import annotations

import pytest

from ducky import agent_keys


@pytest.fixture(autouse=True)
def _clean_ctx(monkeypatch):
    monkeypatch.delenv("AIDUMEI_AGENT_KEY_ENFORCE", raising=False)   # 影子档：围栏仍须生效
    agent_keys.clear_current()
    yield
    agent_keys.clear_current()


def _as_key(callers):
    agent_keys.set_current({"agent": "synthetic-agent", "callers": list(callers)}, "fp-synthetic")


def _as_main():
    agent_keys.set_current(None, "fp-main")


def test_owner_fence_matrix():
    assert agent_keys.owner_fence("bob", "x") is True            # 无钥匙上下文（控制台/本机）
    _as_main()
    assert agent_keys.owner_fence("bob", "x") is True            # 主 token
    _as_key(["alice"])
    assert agent_keys.owner_fence("alice", "x") is True
    assert agent_keys.owner_fence("bob", "x") is False
    assert agent_keys.owner_fence("", "x") is False              # 空目标 = 全实例，钥匙不许


class _Mem:
    def __init__(self):
        self.updated, self.added = [], []

    def search(self, *_a, **_k):
        return {"results": [{"id": "m-1", "memory": "synthetic likes tea", "metadata": {}}]}

    def get(self, _mid):
        return {"id": "m-1", "memory": "synthetic likes tea"}

    def update(self, mid, data, metadata=None):
        self.updated.append((mid, data))

    def get_all(self, *a, **k):
        return {"results": [{"id": f"m-{i}", "memory": f"synthetic note {i}",
                             "metadata": {"category": "pref"}} for i in range(4)]}

    def add(self, messages, **kw):
        self.added.append(messages)
        return {"results": [{"id": "skill-1"}]}


def _force_relation(monkeypatch):
    from ducky import self_edit
    monkeypatch.setattr(self_edit, "SELF_EDIT_ENABLED", True)
    monkeypatch.setattr(self_edit, "_detect_relation", lambda *a, **k: {
        "decision": "duplicate", "memory_id": "m-1", "merged_content": "synthetic likes green tea",
        "confidence": 0.9, "reason": "synthetic"})


def test_self_edit_refused_for_other_identity(monkeypatch):
    from ducky.self_edit import self_edit_on_add
    _force_relation(monkeypatch)
    mem = _Mem()
    _as_key(["alice"])
    out = self_edit_on_add(mem, "bob", [{"role": "user", "content": "synthetic likes green tea"}], {})
    assert out is None and mem.updated == []        # 修复前：bob 的旧记忆被 alice 的钥匙改写


def test_self_edit_allowed_for_own_identity_and_main(monkeypatch):
    from ducky.self_edit import self_edit_on_add
    _force_relation(monkeypatch)
    for setup, user in ((lambda: _as_key(["alice"]), "alice"), (_as_main, "bob"), (agent_keys.clear_current, "bob")):
        setup()
        mem = _Mem()
        out = self_edit_on_add(mem, user, [{"role": "user", "content": "synthetic likes green tea"}], {})
        assert out and out["memory_id"] == "m-1" and mem.updated, user


def test_instinct_scan_and_graduate_refused_for_other_identity(monkeypatch):
    from ducky import instinct_graduation as ig
    mem = _Mem()
    _as_key(["alice"])
    with pytest.raises(PermissionError):
        ig.scan_instincts(mem, "bob")
    with pytest.raises(PermissionError):
        ig.graduate_to_skill(mem, "bob", {"category": "pref", "count": 4})
    with pytest.raises(PermissionError):
        ig.auto_graduate(mem, "bob")
    assert mem.added == []


def test_instinct_scan_allowed_for_own_identity_and_main():
    from ducky import instinct_graduation as ig
    for setup, user in ((lambda: _as_key(["alice"]), "alice"), (_as_main, "bob"), (agent_keys.clear_current, "bob")):
        setup()
        groups = ig.scan_instincts(_Mem(), user)
        assert groups and groups[0]["category"] == "pref", user

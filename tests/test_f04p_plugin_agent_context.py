"""f0.4+: the Hermes plugin honours the host's agent_context contract.

Hermes v0.22 passes ``agent_context`` ("primary" | "subagent" | "cron" |
"flush") to ``MemoryProvider.initialize`` and asks providers to skip writes
for non-primary contexts. The shell hooks follow the same policy through
``platform``; both lines share ``AIDUMEI_SKIP_PLATFORMS``. Reads stay on: a
scheduled job may still recall what it needs.
"""
import json
from urllib.parse import urlsplit

import pytest
from test_f0_3_plugin_lifecycle import plugin as plugin, _stub


def _respond(method, path, body):
    if urlsplit(path).path == "/session/distill":
        return 200, {"status": "ok", "summary": "Synthetic summary", "user_id": "alice", "bank_id": "work"}
    if urlsplit(path).path == "/search":
        return 200, {"results": [{"memory": "synthetic recalled fact", "score": 0.9}]}
    return 200, {"status": "ok", "durable": True}


def _exercise(plugin, context, monkeypatch, skip_env=None):
    if skip_env is None:
        monkeypatch.delenv("AIDUMEI_SKIP_PLATFORMS", raising=False)
    else:
        monkeypatch.setenv("AIDUMEI_SKIP_PLATFORMS", skip_env)
    with _stub(_respond) as (url, calls):
        p = plugin.AiduMemProvider({"url": url, "user_id": "alice", "bank_id": "work"})
        p._client.base = url
        p._spawn = lambda fn, name: fn()
        kwargs = {} if context is None else {"agent_context": context}
        p.initialize("ctx-session", platform="cron" if context == "cron" else "cli", **kwargs)
        remembered = json.loads(p.handle_tool_call("aidumem_remember", {"content": "Synthetic fact"}))
        recalled = json.loads(p.handle_tool_call("aidumem_search", {"query": "synthetic"}))
        for i in range(3):
            p.sync_turn("Synthetic useful turn %d" % i, "reply", session_id="ctx-session")
        p.on_pre_compress([{"role": "user", "content": "Synthetic compressed turn"}])
        p.on_memory_write("replace", "user", "Synthetic profile")
        p.on_session_end([])
    writes = [path for _, path, _ in calls
              if urlsplit(path).path in ("/add", "/facts/add", "/session/distill")]
    return writes, remembered, recalled


@pytest.mark.parametrize("context", ["cron", "subagent", "flush"])
def test_non_primary_agents_read_but_never_write(plugin, monkeypatch, context):
    writes, remembered, recalled = _exercise(plugin, context, monkeypatch)
    assert writes == [], f"{context} agent wrote: {writes}"
    assert "Not stored" in remembered["result"], remembered
    assert "synthetic recalled fact" in recalled["result"], "reads must stay available"


@pytest.mark.parametrize("context", ["primary", None])
def test_primary_agent_still_writes_every_line(plugin, monkeypatch, context):
    """Negative control: the same exercise does produce writes for the primary agent
    (and for hosts that do not pass agent_context at all)."""
    writes, remembered, _ = _exercise(plugin, context, monkeypatch)
    paths = {urlsplit(p).path for p in writes}
    assert {"/add", "/facts/add", "/session/distill"} <= paths, writes
    assert remembered == {"result": "Stored in aiduMEI."}


def test_empty_skip_list_lets_scheduled_agents_write_but_never_flush(plugin, monkeypatch):
    writes, _, _ = _exercise(plugin, "cron", monkeypatch, skip_env="")
    assert writes, "AIDUMEI_SKIP_PLATFORMS='' must let cron agents write"
    flush_writes, _, _ = _exercise(plugin, "flush", monkeypatch, skip_env="")
    assert flush_writes == [], "the host's flush agent duplicates stored turns; always skipped"

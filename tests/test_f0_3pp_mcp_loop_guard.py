"""Issue #23/#24: dependency failures and actual tool dispatch contracts."""
from __future__ import annotations

import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import weakref

import pytest

from ducky.loop_guard import LoopGuard, RETRY_ADVICE, fingerprint
from test_f0_3_plugin_lifecycle import plugin as plugin

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clock():
    return [100.0]


@pytest.fixture
def guard(clock):
    return LoopGuard(clock=lambda: clock[0])


def fail(guard, key="test"):
    token, blocked = guard.begin(key)
    assert blocked is None
    return guard.finish(token, True)


def test_repeated_failures_block_api_then_one_probe_recovers(guard, clock):
    calls = []

    def write(content="sample"):
        calls.append(content)
        return json.dumps({"error": "HTTP 422"})

    wrapped = guard.wrap(write)
    responses = [json.loads(wrapped()) for _ in range(6)]
    assert len(calls) == 5
    assert "retry_count" not in responses[0]
    assert responses[1]["retry_count"] == 2
    assert responses[2]["loop_warning"] == RETRY_ADVICE
    assert responses[5]["error"] == "circuit_open"
    assert responses[5]["retry_after"] == 30
    clock[0] += 30
    key = fingerprint("write", {"content": "sample"})
    probe, blocked = guard.begin(key)
    assert probe is not None and blocked is None
    _, second = guard.begin(key)
    assert second["error"] == "circuit_open"
    guard.finish(probe, False)
    assert guard.begin(key)[1] is None
    assert fail(guard, key) == {}


def test_failed_probe_reopens_even_outside_window(guard, clock):
    for _ in range(5):
        fail(guard)
    clock[0] += 100
    fail(guard)
    assert guard.begin("test")[1]["retry_after"] == 30


def test_sliding_window_and_success_clear(guard, clock):
    for _ in range(4):
        fail(guard)
    clock[0] += 61
    assert fail(guard) == {}
    for _ in range(3):
        fail(guard)
    token, _ = guard.begin("test")
    guard.finish(token, False)
    assert fail(guard) == {}
    # A fixed window beginning at the first failure must not over-count.
    clock[0] += 40
    assert fail(guard)["retry_count"] == 2
    clock[0] += 21
    assert fail(guard)["retry_count"] == 2


def test_normalized_defaults_tenant_tool_and_connection_are_separate(guard):
    scope = ["client-a"]

    def write(content, user_id="tenant-a", bank_id="default"):
        return '{"error":"bad"}'

    wrapped = guard.wrap(write, lambda: scope[0])
    wrapped("bad")
    assert json.loads(wrapped(content="bad", bank_id="default", user_id="tenant-a"))["retry_count"] == 2
    assert "retry_count" not in json.loads(wrapped("bad", user_id="tenant-b"))
    assert "retry_count" not in json.loads(wrapped("bad", bank_id="other"))
    scope[0] = "client-b"
    assert "retry_count" not in json.loads(wrapped("bad"))
    assert fingerprint("read", {"content": "bad"}) != fingerprint("write", {"content": "bad"})
    assert "bad" not in next(iter(guard._states))


def test_successful_batches_unlimited_and_disabled_guard(guard):
    calls = []

    def write():
        calls.append(1)
        return '{"status":"ok"}'

    wrapped = guard.wrap(write)
    for _ in range(80):
        assert json.loads(wrapped())["status"] == "ok"
    assert len(calls) == 80
    guard.enabled = False
    assert len([guard.wrap(lambda: '{"error":"bad"}')() for _ in range(20)]) == 20


@pytest.mark.parametrize("phase", ["begin", "finish"])
def test_guard_fault_open_never_executes_tool_twice(guard, monkeypatch, phase):
    def broken(*args):
        raise ValueError("accounting failed")

    monkeypatch.setattr(guard, phase, broken)
    calls = []

    def write():
        calls.append(1)
        raise OSError("transport failed")

    with pytest.raises(OSError, match="transport failed"):
        guard.wrap(write)()
    assert calls == [1]


def test_tool_exceptions_remain_errors_with_hints(guard):
    calls = []

    def write():
        calls.append(1)
        raise OSError("failed")

    wrapped = guard.wrap(write)
    with pytest.raises(OSError):
        wrapped()
    for count in range(2, 6):
        with pytest.raises(RuntimeError) as exc:
            wrapped()
        assert json.loads(str(exc.value))["retry_count"] == count
    assert json.loads(wrapped())["error"] == "circuit_open"
    assert len(calls) == 5


def test_capacity_and_concurrent_single_probe(guard, clock):
    for n in range(300):
        fail(guard, str(n))
    assert len(guard._states) == 256
    for _ in range(5):
        fail(guard, "hot")
    clock[0] += 30
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: guard.begin("hot"), range(20)))
    assert sum(token is not None for token, _ in results) == 1


def test_async_guard(guard):
    async def write():
        return '{"error":"bad"}'

    async def run():
        fn = guard.wrap(write)
        for _ in range(5):
            await fn()
        return json.loads(await fn())

    assert asyncio.run(run())["error"] == "circuit_open"


@pytest.mark.parametrize("version", ["2.0.0", None])
def test_missing_or_incompatible_sdk_has_install_instruction(monkeypatch, version):
    def get_version(name):
        if version is None:
            raise importlib.metadata.PackageNotFoundError("mcp")
        return version

    monkeypatch.setattr(importlib.metadata, "version", get_version)
    spec = importlib.util.spec_from_file_location("_mcp_bad_sdk", ROOT / "mcp_server.py")
    with pytest.raises(RuntimeError, match="pip install 'mcp==1.30.0'"):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_requirements_lock_official_sdk():
    from packaging.requirements import Requirement
    rows = [Requirement(line.split("#")[0].strip()) for line in
            (ROOT / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    sdk = next(row for row in rows if row.name == "mcp")
    assert str(sdk.specifier) == "==1.30.0"
    assert not any(row.name == "fastmcp" for row in rows)


def test_registered_tools_match_source_and_have_unchanged_schema(monkeypatch):
    import mcp_server as server
    tree = ast.parse((ROOT / "mcp_server.py").read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and
        isinstance(d.func.value, ast.Name) and d.func.value.id == "mcp" and d.func.attr == "tool"
        for d in n.decorator_list)}
    tools = asyncio.run(server.mcp.list_tools())
    assert names and names == {tool.name for tool in tools}
    add = next(tool for tool in tools if tool.name == "mem_add")
    assert set(add.inputSchema["properties"]) == {"messages", "user_id", "bank_id"}
    assert add.inputSchema["required"] == ["messages"]
    monkeypatch.setattr(server, "_api_post", lambda *a, **k: {"error": "HTTP 422"})
    assert "error" in json.loads(server.mem_add("[]", user_id="guard-test"))


@pytest.mark.parametrize("tool,args", [
    ("aidumem_search", {"query": ""}),
    ("aidumem_search", {"query": "sample"}),
    ("aidumem_remember", {"content": ""}),
    ("aidumem_remember", {"content": "sample"}),
    ("aidumem_status", {}),
    ("unknown", {}),
])
def test_plugin_failure_hints_without_circuit_breaking(plugin, monkeypatch, tool, args):
    provider = plugin.AiduMemProvider({"user_id": "synthetic", "bank_id": "test"})
    monkeypatch.setattr(provider._client, "try_request", lambda *a, **k: None)
    responses = [json.loads(provider.handle_tool_call(tool, args)) for _ in range(7)]
    assert "retry_count" not in responses[0]
    assert responses[1]["retry_count"] == 2
    assert responses[2]["loop_warning"] == RETRY_ADVICE
    assert responses[-1]["retry_count"] == 7
    assert responses[-1]["error"] != "circuit_open"


def test_plugin_success_and_new_session_reset_hints(plugin, monkeypatch):
    provider = plugin.AiduMemProvider({"user_id": "synthetic"})
    response = [None]
    monkeypatch.setattr(provider._client, "try_request", lambda *a, **k: response[0])
    args = {"content": "sample"}
    for _ in range(3):
        provider.handle_tool_call("aidumem_remember", args)
    response[0] = {"status": "ok", "durable": True}
    assert "Stored" in json.loads(provider.handle_tool_call("aidumem_remember", args))["result"]
    response[0] = None
    assert "retry_count" not in json.loads(provider.handle_tool_call("aidumem_remember", args))
    provider._session_id = "another-synthetic-session"
    assert "retry_count" not in json.loads(provider.handle_tool_call("aidumem_remember", args))


def test_sse_sessions_use_distinct_weak_scopes(monkeypatch):
    import mcp_server as server
    instance = server._GuardedFastMCP("scope-test")
    class Session:
        pass
    class Context:
        pass
    context = Context()
    first, second = Session(), Session()
    monkeypatch.setattr(instance, "get_context", lambda: context)
    context.session = first
    a = instance._guard_scope()
    assert a == instance._guard_scope()
    context.session = second
    assert a != instance._guard_scope()
    reference = weakref.ref(first)
    del first
    assert reference() is None
    assert len(instance._guard_sessions) == 1


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "invalid"])
def test_invalid_settings_default_and_registered(monkeypatch, value):
    from ducky.env_registry import is_known_env_name
    for suffix in ("THRESHOLD", "WINDOW_S", "COOLDOWN_S"):
        key = "AIDUMEI_MCP_LOOP_GUARD_" + suffix
        monkeypatch.setenv(key, value)
        assert is_known_env_name(key)
    guard = LoopGuard.from_env()
    assert (guard.threshold, guard.window_s, guard.cooldown_s) == (5, 60, 30)

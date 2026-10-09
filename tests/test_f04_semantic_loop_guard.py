"""Schema-declared JSON equivalence, plus actual MCP stdio dispatch evidence."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ducky.loop_guard import LoopGuard, fingerprint

ROOT = Path(__file__).resolve().parents[1]


def key(messages, **kwargs):
    return fingerprint("mem_add", {"messages": messages, **kwargs}, json_fields=("messages",))


def variants():
    payload = [{"role": "user", "content": "原话 exact text", "metadata": {"request_id": "nested"}}]
    text = json.dumps(payload, ensure_ascii=False)
    return [text + " " * i for i in range(6)] + [
        json.dumps(payload, indent=2), json.dumps(payload, sort_keys=True),
        json.dumps(payload, separators=(",", ":")),
        "\n" + text + "\t", json.dumps(payload, ensure_ascii=True), text,
    ]


def test_json_variants_share_one_key_but_business_inputs_do_not():
    assert len({key(v) for v in variants()}) == 1
    base = variants()[0]
    payload = json.loads(base)
    different_content = [{**payload[0], "content": "原话  exact text"}]
    different_role = [{**payload[0], "role": "assistant"}]
    different_nested_id = [{**payload[0], "metadata": {"request_id": "other"}}]
    for other in (different_content, different_role, different_nested_id):
        assert key(base) != key(json.dumps(other))
    assert key(base, user_id="a") != key(base, user_id="b")
    assert key(base, bank_id="a") != key(base, bank_id="b")
    assert key('[1,2]') != key('[2,1]')
    assert key('{"a":true}') != key('{"a":1}')
    assert key("broken") != key('"broken"')
    assert key("NaN") != key('"NaN"')
    assert key('{"a":1,"a":2}') == key('{"a":2}')  # Same decoder as mem_add.
    assert key('["\\ud800"]') == key(' [ "\\ud800" ] ')
    assert fingerprint("other", {"messages": base}) != key(base)
    # JSON-looking *ordinary text* is not silently rewritten.
    assert fingerprint("raw", {"content": "[]"}) != fingerprint("raw", {"content": "[ ]"})


def test_schema_declaration_must_match_tool_parameters():
    with pytest.raises(ValueError, match="tool schema"):
        LoopGuard().wrap(lambda content: content, json_fields=("messages",))


def test_registered_mem_add_decodes_json_without_mutating_original_argument():
    calls = []

    def mem_add(messages, user_id="a", bank_id="default"):
        calls.append(messages)
        return {"error": "synthetic"}

    wrapped = LoopGuard().wrap(mem_add, json_fields=("messages",))
    results = [wrapped(value) for value in variants()]
    assert calls == variants()[:5]
    assert results[1]["retry_count"] == 2
    assert all(json.loads(r)["error"] == "circuit_open" for r in results[5:])


def test_stdio_semantic_variants_stop_at_five_and_distinct_business_calls_dispatch(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            raw = b'{"error":"synthetic unavailable"}'
            self.send_response(503)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AIDUMEM_", "AIDUMEI_")) and not k.lower().endswith("proxy")}
    empty = tmp_path / "empty.env"
    empty.write_text("")
    env.update(AIDUMEM_HOME=str(tmp_path), AIDUMEM_ENV_FILE=str(empty),
               AIDUMEM_DATA_DIR=str(tmp_path / "data"), AIDUMEM_LOG_DIR=str(tmp_path / "logs"),
               AIDUMEM_API_BASE=f"http://127.0.0.1:{http.server_port}",
               AIDUMEM_USER_ID="wire-principal", AIDUMEM_DEFAULT_USER_ID="wire-principal",
               AIDUMEM_HOST_STATE_DB="", AIDUMEM_API_TOKEN="synthetic-token",
               AIDUMEI_MCP_LOOP_GUARD_COOLDOWN_S="60", NO_PROXY="127.0.0.1,localhost")

    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(tool="mem_add", **args):
                    result = await session.call_tool(tool, args)
                    assert not result.isError, result.content
                    return json.loads(result.content[0].text)

                # Exact-input positive control on a distinct bank.
                controls = [await call(messages="[]", user_id="wire-a", bank_id="control") for _ in range(6)]
                assert len(requests) == 5 and controls[5]["error"] == "circuit_open"
                before = len(requests)
                results = [await call(messages=v, user_id="wire-a", bank_id="test") for v in variants()]
                assert len(requests) - before == 5
                assert results[1]["retry_count"] == 2 and "loop_warning" in results[2]
                assert all(r["error"] == "circuit_open" for r in results[5:])
                sent = requests[before:]
                assert all(body["messages"] == json.loads(variants()[0]) for _, body in sent)
                assert all(body["caller_user_id"] == "wire-principal" for _, body in sent)
                # Tenant, bank, content whitespace and different tool each have
                # their own failure count; no unrelated operation is blocked.
                base = variants()[0]
                controls = [
                    await call(messages=base, user_id="wire-b", bank_id="test"),
                    await call(messages=base, user_id="wire-a", bank_id="another"),
                    await call(messages=base.replace("exact text", "exact  text"), user_id="wire-a", bank_id="test"),
                    await call("mem_add_raw", content=base, user_id="wire-a", bank_id="test"),
                ]
                assert len(requests) == 14
                assert all(r["error"] != "circuit_open" and "retry_count" not in r for r in controls)

    try:
        asyncio.run(scenario())
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)

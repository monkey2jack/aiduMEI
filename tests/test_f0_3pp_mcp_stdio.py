"""Exercise the real SDK wire protocol, not just a Python import."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def test_stdio_breaker_stops_http_and_recovers(tmp_path):
    requests = []
    response = [{"error": "synthetic service failure"}]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            raw = json.dumps(response[0]).encode()
            self.send_response(503 if "error" in response[0] else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AIDUMEM_", "AIDUMEI_")) and not k.lower().endswith("proxy")}
    env.update(AIDUMEM_HOME=str(ROOT), AIDUMEM_ENV_FILE=str(tmp_path / "empty.env"),
               AIDUMEM_DATA_DIR=str(tmp_path / "data"), AIDUMEM_LOG_DIR=str(tmp_path / "logs"),
               AIDUMEM_API_BASE=f"http://127.0.0.1:{http.server_port}",
               # A real subprocess round trip can exceed 100 ms on a busy host.
               # Keep the circuit open through the rejection assertion, then
               # exercise recovery using the actual wire retry_after contract.
               AIDUMEI_MCP_LOOP_GUARD_COOLDOWN_S="5", NO_PROXY="127.0.0.1,localhost")
    (tmp_path / "empty.env").write_text("")

    async def run():
        parameters = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], env=env)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert any(t.name == "mem_add" for t in listed.tools)
                async def call():
                    result = await session.call_tool("mem_add", {"messages": "[]", "user_id": "wire-test"})
                    assert not result.isError, result.content
                    return json.loads(result.content[0].text)
                results = [await call() for _ in range(6)]
                assert len(requests) == 5
                assert results[1]["retry_count"] == 2
                assert "loop_warning" in results[2]
                assert results[5]["error"] == "circuit_open"
                assert 1 <= results[5]["retry_after"] <= 5
                await asyncio.sleep(results[5]["retry_after"] + 0.05)
                response[0] = {"status": "ok", "durable": True}
                assert (await call())["status"] == "ok"
                response[0] = {"error": "synthetic service failure"}
                assert "retry_count" not in await call()
                assert len(requests) == 7

    try:
        asyncio.run(run())
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)

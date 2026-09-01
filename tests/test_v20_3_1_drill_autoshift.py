"""v20.3.1（九份审计 P0-4 · 嘟嘟 🔴-2）：drill_autoshift.sh --run 必须真的能跑通。

上一版的病（从写出来那天起没成功跑通一次）：
  1. heredoc 与管道抢 stdin → sys.stdin.read() 得空串 → JSONDecodeError；
  2. 断言的 engine_mode / llm_gear / pending_replay 在 /health 顶层不存在
     （真身在 probes.* 下）→ 修好 stdin 也是全 FAIL；
  3. AUTH_ARGS=() 空数组在 bash 3.2 + set -u 下 unbound variable。

判据落在真实进程行为上：起一台只回合法 /health 的本地 mock 服务，
drill --run 必须输出 status=pass 且 exit 0。acceptance 的 `test -x` 只量
「文件在不在」，量不出「功能通不通」—— 嘟嘟原话：尺子量了存在性，
没量可行性。
"""

import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DRILL = os.path.join(_ROOT, "scripts", "drill_autoshift.sh")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _MockHealth(BaseHTTPRequestHandler):
    received_auth: list = []

    def do_GET(self):
        _MockHealth.received_auth.append(self.headers.get("Authorization"))
        body = json.dumps({
            "status": "ok", "version": "20.3.1", "health_status": "ok",
            "degraded": [], "warming_up": [],
            "probes": {
                "engine_mode_policy": {"configured": "auto", "mode": "auto"},
                "llm_gear": {"mode": "full", "breaker": "closed"},
                "pending_embeddings": {"pending_count": 0},
            },
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def mock_service():
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _MockHealth)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_drill_run_passes_against_real_health_shape(tmp_path, mock_service):
    """决定性判据：--run 打真实形状的 /health，status=pass 且 exit 0。"""
    out_file = tmp_path / "drill.json"
    env = {**os.environ,
           "AIDUMEM_API_BASE": mock_service,
           "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
           "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
           "OUT": str(out_file)}
    r = subprocess.run(["bash", _DRILL, "--run"],
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, f"--run 崩了:\nstdout:{r.stdout[-400:]}\nstderr:{r.stderr[-400:]}"
    result = json.loads(out_file.read_text())
    assert result["status"] == "pass", result
    assert all(result["checks"].values()), result["checks"]
    assert result["engine_mode"] == "auto", "engine_mode 没从 probes.engine_mode_policy 读到"


def test_drill_run_with_token_passes_and_token_not_on_command_line(tmp_path, mock_service):
    """带 token 的路径也必须通（bash 3.2 空数组旧病），且 token 不进 ps。"""
    _MockHealth.received_auth.clear()
    out_file = tmp_path / "drill_auth.json"
    env = {**os.environ,
           "AIDUMEM_API_BASE": mock_service,
           "AIDUMEM_API_TOKEN": "secret-token-123",
           "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
           "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
           "OUT": str(out_file)}
    r = subprocess.run(["bash", _DRILL, "--run"],
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, f"带 token 的 --run 崩了: {r.stderr[-300:]}"
    # 脚本会打两次 /health：先探活（无 token），再取数据（带 token）。
    # 第一次的 None 是探活请求的正确行为，不是 token 丢失。
    authed = [a for a in _MockHealth.received_auth if a is not None]
    assert authed == ["Bearer secret-token-123"], (
        f"token 没经 stdin 正确到达服务端（收到的 Authorization 序列："
        f"{_MockHealth.received_auth}）"
    )


def test_drill_run_fails_when_service_unreachable(tmp_path):
    """负向对照：服务不可达必须 exit 3 —— 尺子两头都要量。"""
    env = {**os.environ,
           "AIDUMEM_API_BASE": "http://127.0.0.1:1",  # 不可达端口
           "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
           "AIDUMEM_LOG_DIR": str(tmp_path / "logs")}
    r = subprocess.run(["bash", _DRILL, "--run"],
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 3, f"不可达必须 exit 3, got {r.returncode}"


def test_drill_check_verifies_key_names_statically():
    """--check 顺手静态断言 --run 要读的键在 health.py 里存在（嘟嘟 🟢-2）。"""
    env = {**os.environ, "AIDUMEM_DATA_DIR": "/tmp/aidumei_drill_t_data",
           "AIDUMEM_LOG_DIR": "/tmp/aidumei_drill_t_logs"}
    r = subprocess.run(["bash", _DRILL, "--check"],
                       capture_output=True, text=True, timeout=15, env=env)
    assert r.returncode == 0, r.stderr[-300:]
    assert "PASS" in r.stdout

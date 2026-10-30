"""scripts/aidumei_watchdog.sh 的拉起分支真的能把服务拉起来。

此前拉起调用的是仓库里并不存在的 start-aidumei.sh（这一步必失败），而那一行的
`&>>` 是 bash 4 语法 —— macOS 自带 /bin/bash 3.2 解析整份脚本即报语法错误，连探活都
不会执行。这里起一个假的安装目录：venv/bin/python 是桩，被拉起后在指定端口回 /health 200。
"""
from __future__ import annotations

import os
import re
import signal
import socket
import stat
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_WATCHDOG = _ROOT / "scripts" / "aidumei_watchdog.sh"

_STUB = """#!/bin/sh
echo "$PWD $*" > "{marker}"
echo $$ > "{pidfile}"
exec "{python}" -c '
import http.server, os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"{{}}")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(os.environ["WD_PORT"])), H).serve_forever()
'
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_watchdog_restarts_api_from_the_install_dir(tmp_path):
    install = tmp_path / "install"
    (install / "venv" / "bin").mkdir(parents=True)
    marker, pidfile = tmp_path / "started", tmp_path / "stub.pid"
    stub = install / "venv" / "bin" / "python"
    stub.write_text(_STUB.format(marker=marker, pidfile=pidfile, python=sys.executable), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    port = _free_port()
    env = {**os.environ, "AIDUMEI_INSTALL_DIR": str(install), "AIDUMEI_URL": f"http://127.0.0.1:{port}",
           "AIDUMEI_QDRANT_CONTAINER": "aidumei-test-no-such-container", "WD_PORT": str(port),
           "AIDUMEI_SYSTEMD_UNITS": "aidumei-test-no-such-unit.service"}
    env.pop("AIDUMEM_API_TOKEN", None)
    try:
        r = subprocess.run(["bash", str(_WATCHDOG)], env=env, capture_output=True, text=True, timeout=60)
        log = (install / "logs" / "watchdog.log").read_text(encoding="utf-8", errors="replace")
        assert r.returncode == 0, (r.stderr, log)
        assert "已复活" in log, log
        cwd, args = marker.read_text(encoding="utf-8").split(" ", 1)
        assert Path(cwd).resolve() == install.resolve() and args.strip() == "api_server.py"
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
            except ProcessLookupError:
                pass


_BASH4_ONLY = re.compile(r"&>>|\bmapfile\b|\breadarray\b|\bdeclare\s+-A\b|\$\{\w+(,,|\^\^)\}")


def test_shell_scripts_avoid_bash4_only_syntax():
    """macOS 自带 /bin/bash 是 3.2：一处 bash 4 语法就让整份脚本解析失败。"""
    hits = []
    for path in sorted([*_ROOT.glob("scripts/*.sh"), *_ROOT.glob("integrations/**/*.sh")]):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith("#") and _BASH4_ONLY.search(line):
                hits.append(f"{path.relative_to(_ROOT)}:{n}: {line.strip()}")
    assert not hits, "\n".join(hits)


def test_watchdog_parses_with_the_system_bash():
    """系统 /bin/bash（macOS 上是 3.2）必须能解析 watchdog。"""
    r = subprocess.run(["/bin/bash", "-n", str(_WATCHDOG)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_watchdog_does_not_start_a_second_process_when_systemd_owns_the_unit(tmp_path):
    """f0.7.1：单元归 systemd 管（active/activating）时，只留痕不拉起 —— 否则双进程抢端口与 API 锁。"""
    install = tmp_path / "install"
    (install / "venv" / "bin").mkdir(parents=True)
    marker = tmp_path / "started"
    stub = install / "venv" / "bin" / "python"
    stub.write_text(f"#!/bin/sh\necho started > {marker}\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake = fakebin / "systemctl"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = "aiduMEI.service" ] && { echo activating; exit 0; }; done\necho inactive; exit 3\n',
                    encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "AIDUMEI_INSTALL_DIR": str(install), "AIDUMEI_URL": f"http://127.0.0.1:{_free_port()}",
           "AIDUMEI_QDRANT_CONTAINER": "aidumei-test-no-such-container",
           "PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"}
    env.pop("AIDUMEM_API_TOKEN", None)
    env.pop("AIDUMEI_SYSTEMD_UNITS", None)
    r = subprocess.run(["bash", str(_WATCHDOG)], env=env, capture_output=True, text=True, timeout=60)
    log = (install / "logs" / "watchdog.log").read_text(encoding="utf-8", errors="replace")
    assert r.returncode == 0, (r.stderr, log)
    assert "由 systemd" in log and "不拉起" in log, log
    assert not marker.exists(), "systemd 管着单元时 watchdog 仍拉起了第二个进程"

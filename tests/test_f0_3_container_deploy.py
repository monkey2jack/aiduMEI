"""f0.3 container deployment: compose healthcheck, writable config, credentials.

Verified defects (statically, from the shipped files):

* docker-compose.yml's healthcheck ran ``curl`` inside python:3.12-slim, which
  has no curl -> the container could never become healthy;
* the Dockerfile pinned AIDUMEM_CONFIG_FILE=/app/mem0_config_local.json under
  the root-owned, read-only code directory, and compose mounted that file
  ``:ro`` -> every config save (console, PUT /config/*) failed in containers;
* compose bound 0.0.0.0 with the credential lines commented out -> the
  service's fail-closed startup gate refused to start, in a restart loop;
* no vector backend was spelled out for the container.

Where the behaviour can be exercised without Docker it is: the healthcheck
command is executed against a local HTTP server with a PATH that only offers
``python`` (as in the slim image), the compose environment is interpolated the
way compose does and fed to the service's real startup gate, and the config
writer is run against writable and read-only directories. Each fix is paired
with a negative control built from the previous files.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"
EXAMPLE_CONFIG = ROOT / "mem0_config_local.json.example"

# The previous (faaa9af) values, kept as negative controls.
OLD_HEALTHCHECK = ["CMD", "curl", "-sf", "http://localhost:8767/livez"]
OLD_CONFIG_FILE = "/app/mem0_config_local.json"
OLD_VOLUMES = ["./data:/app/data", "./logs:/app/logs",
               "./mem0_config_local.json:/app/mem0_config_local.json:ro"]
OLD_ENVIRONMENT = ["AIDUMEM_HOST=0.0.0.0", "AIDUMEM_API_PORT=8767", "MEM0_API_PORT=8767",
                   "MEM0_TELEMETRY=false"]


def _service() -> dict:
    with COMPOSE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]["aidumem"]


def _dockerfile_instructions() -> list[str]:
    """Dockerfile instructions with line continuations joined, comments dropped."""
    joined, current = [], ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        joined.append(current + line)
        current = ""
    return joined


def _dockerfile_env() -> dict[str, str]:
    env = {}
    for ins in _dockerfile_instructions():
        if ins.upper().startswith("ENV "):
            key, _, value = ins[4:].strip().partition("=")
            env[key.strip()] = shlex.split(value)[0] if value else ""
    return env


def _dockerfile_healthcheck() -> list[str]:
    ins = next(i for i in _dockerfile_instructions() if i.upper().startswith("HEALTHCHECK "))
    return shlex.split(ins[ins.upper().index(" CMD ") + 5:])


# --------------------------------------------------------------------------
# A small model of compose variable interpolation (${VAR}, :-, -, :?, ?)
# --------------------------------------------------------------------------

class ComposeRefusesToStart(Exception):
    pass


_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}")


def _interpolate(value: str, host_env: dict[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        name, op, arg = match.group(1), match.group(2), match.group(3) or ""
        current = host_env.get(name)
        if op == ":-":
            return current if current else arg
        if op == "-":
            return current if current is not None else arg
        if op == ":?" and not current:
            raise ComposeRefusesToStart(arg or name)
        if op == "?" and current is None:
            raise ComposeRefusesToStart(arg or name)
        return current or ""
    return _INTERPOLATION.sub(substitute, value)


def _container_env(environment: list[str], host_env: dict[str, str]) -> dict[str, str]:
    rendered = {}
    for item in environment:
        key, _, value = item.partition("=")
        rendered[key] = _interpolate(value, host_env)
    return rendered


# --------------------------------------------------------------------------
# Healthcheck
# --------------------------------------------------------------------------

class _Livez(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        status = self.server.livez_status if self.path == "/livez" else 404
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def livez_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Livez)
    server.livez_status = 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def slim_path(tmp_path: Path) -> str:
    """A PATH like python:3.12-slim's: python is there, curl and wget are not."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python").symlink_to(sys.executable)
    return str(bindir)


def _run_probe(cmd: list[str], port: int, path: str) -> int:
    assert cmd[0] == "CMD", cmd
    argv = [arg.replace("127.0.0.1:8767", f"127.0.0.1:{port}")
               .replace("localhost:8767", f"127.0.0.1:{port}") for arg in cmd[1:]]
    try:
        return subprocess.run(argv, env={"PATH": path}, capture_output=True,
                              timeout=20).returncode
    except FileNotFoundError:
        return 127  # what the container runtime reports: executable not found


@pytest.mark.parametrize("source", ["compose", "dockerfile"])
def test_healthcheck_probes_livez_with_python_only(source, livez_server, slim_path) -> None:
    cmd = _service()["healthcheck"]["test"] if source == "compose" else ["CMD", *_dockerfile_healthcheck()]
    assert not {"curl", "wget"} & {part.split("/")[-1] for part in cmd}
    assert any("127.0.0.1:8767/livez" in part for part in cmd), cmd
    port = livez_server.server_address[1]

    assert _run_probe(cmd, port, slim_path) == 0
    livez_server.livez_status = 503
    assert _run_probe(cmd, port, slim_path) != 0


def test_compose_and_dockerfile_run_the_same_probe() -> None:
    assert _service()["healthcheck"]["test"][1:] == _dockerfile_healthcheck()


def test_negative_control_old_curl_healthcheck_can_never_pass(livez_server, slim_path) -> None:
    port = livez_server.server_address[1]
    # The service is up and /livez answers 200, yet the old probe cannot even start.
    assert _run_probe(OLD_HEALTHCHECK, port, slim_path) == 127


# --------------------------------------------------------------------------
# Writable configuration path
# --------------------------------------------------------------------------

def _parse_volumes(volumes: list[str]) -> list[tuple[str, str, str]]:
    out = []
    for item in volumes:
        parts = item.split(":")
        source, target = parts[0], parts[1]
        mode = parts[2] if len(parts) > 2 else "rw"
        out.append((source, posixpath.normpath(target), mode))
    return out


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _config_is_writable(config_file: str, volumes: list[str], dockerfile: list[str]) -> bool:
    """The config writer needs its *parent directory* writable (temp file + rename)."""
    config_file = posixpath.normpath(config_file)
    parent = posixpath.dirname(config_file)
    mounts = _parse_volumes(volumes)
    if any("ro" in mode.split(",") and (_within(config_file, target) or _within(parent, target))
           for _src, target, mode in mounts):
        return False
    rw_mount = any(_within(parent, target) for _src, target, mode in mounts
                   if "ro" not in mode.split(","))
    chowned = any(ins.upper().startswith("RUN ") and "chown" in ins
                  and re.search(rf"(?<![\w/]){re.escape(parent)}(?![\w/])", ins)
                  for ins in dockerfile)
    return rw_mount and chowned


def test_container_config_path_lives_in_the_writable_data_mount() -> None:
    service = _service()
    image_default = _dockerfile_env()["AIDUMEM_CONFIG_FILE"]
    compose_value = _container_env(service["environment"], {"AIDUMEM_API_TOKEN": "t"})[
        "AIDUMEM_CONFIG_FILE"]
    assert image_default == compose_value == "/app/data/mem0_config_local.json"
    assert _dockerfile_env()["AIDUMEM_DATA_DIR"] == posixpath.dirname(image_default)
    assert _config_is_writable(compose_value, service["volumes"], _dockerfile_instructions())
    assert not any(":ro" in v or v.endswith(":ro") for v in service["volumes"])


def test_negative_control_old_layout_made_the_config_read_only() -> None:
    assert not _config_is_writable(OLD_CONFIG_FILE, OLD_VOLUMES, _dockerfile_instructions())
    # Even without the :ro file mount, the parent (/app) is root-owned code.
    assert not _config_is_writable(OLD_CONFIG_FILE, OLD_VOLUMES[:2], _dockerfile_instructions())


def test_config_writer_creates_its_temp_file_in_the_config_directory(
        tmp_path: Path, monkeypatch) -> None:
    """Why the parent directory must be writable: temp file there + os.replace."""
    import json
    import tempfile

    from ducky import routes_config

    target = tmp_path / "data" / "mem0_config_local.json"
    target.parent.mkdir()
    monkeypatch.setattr(routes_config, "_CFG_PATH", str(target))
    dirs, replaced = [], []
    real_mkstemp, real_replace = tempfile.mkstemp, os.replace

    def spy_mkstemp(*args, **kwargs):
        dirs.append(kwargs.get("dir"))
        return real_mkstemp(*args, **kwargs)

    def spy_replace(src, dst):
        replaced.append((os.path.dirname(src), dst))
        return real_replace(src, dst)

    monkeypatch.setattr(routes_config.tempfile, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(routes_config.os, "replace", spy_replace)
    routes_config._atomic_write_config({"_speed": {"k": 1}})

    assert dirs == [str(target.parent)]
    assert replaced == [(str(target.parent), str(target))]
    assert json.loads(target.read_text(encoding="utf-8")) == {"_speed": {"k": 1}}


def test_non_container_default_config_path_is_unchanged(monkeypatch) -> None:
    from ducky import utils

    monkeypatch.delenv("AIDUMEM_CONFIG_FILE", raising=False)
    assert utils.mem0_config_path() == os.path.join(utils.BASE_DIR, "mem0_config_local.json")
    monkeypatch.setenv("AIDUMEM_CONFIG_FILE", "/app/data/mem0_config_local.json")
    assert utils.mem0_config_path() == "/app/data/mem0_config_local.json"


# --------------------------------------------------------------------------
# Credentials: compose must refuse before the service does
# --------------------------------------------------------------------------

def _startup_gate(monkeypatch, container_env: dict[str, str]) -> None:
    import api_server
    from ducky.security import auth

    for key in ("AIDUMEM_API_TOKEN", "AIDUMEM_UI_PASSWORD", "AIDUMEM_ALLOW_INSECURE_PUBLIC",
                "AIDUMEI_TRUST_PROXY", "AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH", "AIDUMEM_HOST"):
        monkeypatch.delenv(key, raising=False)
    for key, value in container_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["aidumem"])
    # The container starts with an empty data volume: no stored password hash.
    monkeypatch.setattr(auth, "ui_password_configured",
                        lambda: bool(os.environ.get("AIDUMEM_UI_PASSWORD", "").strip()))
    api_server._enforce_public_binding_policy()


def test_compose_refuses_to_start_without_a_token() -> None:
    with pytest.raises(ComposeRefusesToStart) as refused:
        _container_env(_service()["environment"], {})
    assert "AIDUMEM_API_TOKEN" in str(refused.value)


def test_compose_environment_with_a_token_passes_the_real_startup_gate(monkeypatch) -> None:
    env = _container_env(_service()["environment"], {"AIDUMEM_API_TOKEN": "synthetic-token"})
    assert env["AIDUMEM_HOST"] == "0.0.0.0"
    assert env["AIDUMEM_API_TOKEN"] == "synthetic-token"
    assert env["AIDUMEM_UI_PASSWORD"] == ""  # optional, empty means unset
    _startup_gate(monkeypatch, env)  # does not raise


def test_negative_control_old_compose_environment_crash_looped(monkeypatch) -> None:
    env = _container_env(OLD_ENVIRONMENT, {"AIDUMEM_API_TOKEN": "in-.env-but-never-referenced"})
    assert "AIDUMEM_API_TOKEN" not in env  # compose never passed it into the container
    with pytest.raises(RuntimeError):
        _startup_gate(monkeypatch, env)


# --------------------------------------------------------------------------
# Vector backend
# --------------------------------------------------------------------------

def test_compose_uses_embedded_qdrant_inside_the_data_volume() -> None:
    import json

    with COMPOSE.open(encoding="utf-8") as fh:
        services = yaml.safe_load(fh)["services"]
    assert set(services) == {"aidumem"}  # no separate vector service to configure
    env = _container_env(services["aidumem"]["environment"], {"AIDUMEM_API_TOKEN": "t"})
    assert env["AIDUMEM_VECTOR_BACKEND"] == "qdrant"

    workdir = next(i.split(None, 1)[1] for i in _dockerfile_instructions()
                   if i.upper().startswith("WORKDIR "))
    data_dir = _dockerfile_env()["AIDUMEM_DATA_DIR"]
    example = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    vector_path = example["vector_store"]["config"]["path"]
    assert "host" not in example["vector_store"]["config"]  # local mode, not a server
    for relative in (vector_path, example["history_db_path"]):
        resolved = posixpath.normpath(posixpath.join(workdir, relative))
        assert _within(resolved, data_dir), (relative, resolved)
    mounts = [target for _s, target, _m in _parse_volumes(services["aidumem"]["volumes"])]
    assert data_dir in mounts


# --------------------------------------------------------------------------
# Documentation of the prerequisites
# --------------------------------------------------------------------------

@pytest.mark.parametrize("readme", ["README.md", "README_EN.md"])
def test_readme_documents_container_prerequisites(readme: str) -> None:
    text = (ROOT / readme).read_text(encoding="utf-8")
    heading = re.search(r"^### .*Docker Compose.*$", text, re.M)
    assert heading, f"{readme} has no container deployment section"
    section = text[heading.start():]
    section = section[:section.index("\n## ")]
    for needle in ("AIDUMEM_API_TOKEN", "chown -R 10001:10001 data logs",
                   "data/mem0_config_local.json", "vector_store.config.path", "/livez"):
        assert needle in section, (readme, needle)

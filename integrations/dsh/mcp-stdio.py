"""DSH stdio launcher for the existing aiduMEI MCP tools (no local API server)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys
from urllib.parse import urlsplit


def _production_token(env: dict[str, str]) -> str:
    """Resolve only the credential; do not import or load production settings."""
    token_file = env.get("AIDUMEI_API_TOKEN_FILE", "").strip()
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").strip()
    token = env.get("AIDUMEM_API_TOKEN", "").strip()
    if token:
        return token
    home = env.get("AIDUMEM_HOME", "").strip()
    candidates = (
        env.get("AIDUMEM_ENV_FILE", ""),
        str(Path(home) / ".env") if home else "",
        str(Path.home() / ".aidumem/.env"),
        # No "./.env": DSH starts MCP inside the user's workspace, and a cloned
        # repository must not be able to supply the credential (same rule as
        # the CC hooks, outside audit R3-01).
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            lines = Path(candidate).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if line.startswith(("export ", "export\t")):
                line = line[len("export"):].strip()
            key, sep, value = line.partition("=")
            if sep and key.strip() == "AIDUMEM_API_TOKEN":
                token = value.strip().strip('"').strip("'")
                if token:
                    return token
    return ""


def prepare(env: dict[str, str]) -> tuple[dict[str, str], Path]:
    """Test endpoints never consult production credentials or .env files."""
    resolved = dict(env)
    test_url = env.get("AIDUMEI_TEST_URL", "").strip()
    api = (test_url or env.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767")).rstrip("/")
    parsed = urlsplit(api)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_api_base")
    if test_url:
        token = env.get("AIDUMEI_TEST_TOKEN", "").strip()
        token_file = env.get("AIDUMEI_TEST_TOKEN_FILE", "").strip()
        if token_file:
            token = Path(token_file).read_text(encoding="utf-8").strip()
    else:
        token = _production_token(env)
    # mcp_server attaches the resolved credential as Authorization: Bearer.
    user = env.get("AIDUMEM_USER_ID", "default").strip() or "default"
    bank = env.get("AIDUMEI_DEFAULT_BANK_ID", "").strip() or env.get("AIDUMEI_BANK_ID", "").strip() or "default"
    scope = hashlib.sha256(json.dumps([api, user, bank, bool(test_url)]).encode()).hexdigest()[:24]
    state_home = Path(env.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) if sys.platform == "win32" else Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    state = state_home / "aiduMEI" / "dsh" / "mcp" / scope
    resolved.update({
        "AIDUMEM_API_BASE": api,
        "AIDUMEM_API_TOKEN": token,
        "AIDUMEM_USER_ID": user,
        "AIDUMEM_DEFAULT_USER_ID": user,
        "AIDUMEI_DEFAULT_BANK_ID": bank,
        "AIDUMEM_ENV_FILE": os.devnull,
        "AIDUMEM_DATA_DIR": str(state / "data"),
        "AIDUMEM_LOG_DIR": str(state / "logs"),
        "AIDUMEI_MCP_DANGEROUS_TOOLS": "0",
        "AIDUMEM_HOST_STATE_DB": "",
    })
    repo = Path(env.get("AIDUMEI_REPO", str(Path(__file__).resolve().parents[2])))
    return resolved, repo


def main() -> int:
    if os.environ.get("AIDUMEI_HOOKS", "").strip().lower() in {"off", "0", "false"}:
        return 0
    try:
        env, repo = prepare(dict(os.environ))
        if not (repo / "mcp_server.py").is_file():
            raise ValueError("repository_not_found")
        if "--check" in sys.argv:
            missing = [name for name in ("mcp", "httpx", "numpy") if importlib.util.find_spec(name) is None]
            if not missing:
                os.environ.update(env)
                sys.path.insert(0, str(repo))
                runpy.run_path(str(repo / "mcp_server.py"), run_name="_aidumei_dsh_check")
            print(json.dumps({"ok": not missing, "missing": missing, "transport": "stdio", "test_mode": bool(env.get("AIDUMEI_TEST_URL"))}))
            return 1 if missing else 0
        os.environ.update(env)
        sys.path.insert(0, str(repo))
        sys.argv = [str(repo / "mcp_server.py")]
        runpy.run_path(str(repo / "mcp_server.py"), run_name="__main__")
        return 0
    except (ValueError, OSError, ImportError):
        # Values and paths can contain credentials. Keep startup errors coded.
        print("[aiduMEI/dsh] mcp_configuration_unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Per-agent API keys for aiduMEI (f0.7+, closes ext-audit S3).

The server stores only sha256(key) in <DATA_DIR>/agent_keys.json (0600).
A new key is written once to a 0600 file you choose and never printed.

  python scripts/agent_keys.py create --agent NAME --callers a,b --out FILE
  python scripts/agent_keys.py bind-main --agent NAME --callers a,b   # bind AIDUMEM_API_TOKEN
  python scripts/agent_keys.py list
  python scripts/agent_keys.py revoke --agent NAME
  python scripts/agent_keys.py events [--days 7]                      # identity mismatches seen

Enforcement: AIDUMEI_AGENT_KEY_ENFORCE=0 (default) only counts mismatches;
=1 refuses a key used for an identity it is not bound to.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _callers(raw: str) -> list[str]:
    out = sorted({c.strip() for c in (raw or "").split(",") if c.strip()})
    if not out:
        raise SystemExit("--callers must name at least one user_id")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--agent", required=True)
    c.add_argument("--callers", required=True)
    c.add_argument("--out", required=True, help="file that receives the key (created 0600, must not exist)")
    b = sub.add_parser("bind-main")
    b.add_argument("--agent", required=True)
    b.add_argument("--callers", required=True)
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("--agent", required=True)
    e = sub.add_parser("events")
    e.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)

    from ducky import agent_keys
    if a.cmd == "create":
        callers = _callers(a.callers)          # validate before anything touches disk
        if os.path.exists(a.out):
            raise SystemExit(f"refusing to overwrite {a.out}")
        token = agent_keys.new_token()
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
        try:
            entry = agent_keys.add_key(a.agent, callers, token)
        except Exception:                        # never leave a key file the server does not know
            os.unlink(a.out)
            raise
        print(json.dumps({"created": entry, "key_file": a.out, "keys_file": agent_keys.keys_path()},
                         ensure_ascii=False))
        return 0
    data = agent_keys.load()
    if a.cmd == "bind-main":
        data = dict(data)
        data["main"] = {"agent": a.agent, "callers": _callers(a.callers)}
        agent_keys.save(data)
        print(json.dumps({"main": data["main"]}, ensure_ascii=False))
        return 0
    if a.cmd == "list":
        print(json.dumps({"keys_file": agent_keys.keys_path(), "main": data.get("main"),
                          "keys": [{k: v for k, v in e.items() if k != "sha256"} for e in data.get("keys", [])],
                          "enforce": agent_keys.enforce_on()}, ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "revoke":
        keep = [e for e in data.get("keys", []) if e.get("agent") != a.agent]
        if len(keep) == len(data.get("keys", [])):
            raise SystemExit(f"no key for agent {a.agent}")
        agent_keys.save({**data, "keys": keep})
        print(json.dumps({"revoked": a.agent}))
        return 0
    if a.cmd == "events":
        from datetime import datetime, timedelta, timezone
        from ducky.write_policy import connect
        since = (datetime.now(timezone.utc) - timedelta(days=a.days)).strftime("%Y-%m-%d")
        conn = connect()
        rows = [dict(x) for x in conn.execute(
            "SELECT day, key_agent, target_user, caller, action, outcome, n FROM key_events WHERE day>=?"
            " ORDER BY day DESC, n DESC", (since,))]
        conn.close()
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

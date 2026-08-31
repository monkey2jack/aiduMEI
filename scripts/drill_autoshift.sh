#!/bin/bash
# Non-destructive autoshift drill for the running aiduMEI instance.
# Modes:
#   --check  verify script contract without contacting the service
#   --run    verify live health fields and emit JSON (default)
set -euo pipefail

ROOT="${AIDUMEM_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
API_BASE="${AIDUMEM_API_BASE:-http://127.0.0.1:8767}"
MODE="${1:---run}"
OUT="${OUT:-/tmp/aidumei_autoshift_drill.json}"
TOKEN="${AIDUMEM_API_TOKEN:-}"

if [[ "${MODE}" == "--check" ]]; then
  test -x "${ROOT}/scripts/e2e_smoke.py"
  test -x "${ROOT}/scripts/report.py"
  test -x "${ROOT}/scripts/drill_autoshift.sh"
  echo "PASS: autoshift drill contract present"
  exit 0
elif [[ "${MODE}" != "--run" ]]; then
  echo "usage: $0 [--check|--run]" >&2
  exit 2
fi

if ! curl -fsS "${API_BASE}/health" >/dev/null; then
  echo "FAIL: service is not reachable at ${API_BASE}" >&2
  exit 3
fi

AUTH_ARGS=()
if [[ -n "${TOKEN}" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${TOKEN}")
fi

HEALTH=$(curl -fsS "${AUTH_ARGS[@]}" "${API_BASE}/health")
printf '%s' "${HEALTH}" | python3 - "${OUT}" <<'PY'
import json, sys
health = json.loads(sys.stdin.read())
checks = {
    "engine_mode_present": isinstance(health.get("engine_mode"), str),
    "llm_gear_present": isinstance(health.get("llm_gear"), dict),
    "pending_replay_present": "pending_replay" in health,
    "warming_up_present": isinstance(health.get("warming_up"), list),
    "degraded_present": isinstance(health.get("degraded"), list),
}
result = {
    "status": "pass" if all(checks.values()) else "fail",
    "checks": checks,
    "engine_mode": health.get("engine_mode"),
    "llm_gear": health.get("llm_gear"),
    "pending_replay": health.get("pending_replay"),
    "warming_up": health.get("warming_up"),
    "degraded": health.get("degraded"),
}
with open(sys.argv[1], "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2))
if result["status"] != "pass":
    raise SystemExit(3)
PY

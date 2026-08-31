#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
errors=0

check() {
  local description="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf 'PASS %s\n' "$description"
  else
    printf 'FAIL %s\n' "$description" >&2
    errors=$((errors+1))
  fi
}

cd "$ROOT"

check "entry files exist" bash -c '
  test -f AGENTS.md &&
  test -f llms.txt &&
  test -f TROUBLESHOOTING.md &&
  test -f docs/OPERATIONS.md &&
  test -f docs/HEALTH.md &&
  test -f docs/AGENT_INTEGRATION.md &&
  test -f docs/BACKUP_RESTORE.md &&
  test -f scripts/README.md
'
check "README line count <=600" bash -c 'test "$(wc -l < README.md)" -le 600'
check "AGENTS.md <=12KB" bash -c 'test "$(wc -c < AGENTS.md)" -le 12000'
check "rerank example is nested" grep -q '"config": {' mem0_config_local.json.example
check "no hardcoded restore date" bash -c '! grep -q 20260727 scripts/restore_backup.py'
check "no hardcoded green probes" bash -c '! grep -q "\"injection_guard_ok\": True" ducky/hot/health.py && ! grep -q "\"port_service\": True" ducky/hot/health.py'
check "MCP port is 8766 in README" bash -c '! grep -q ":8768" README.md && ! grep -q ":8768" README_EN.md'
check "e2e script is executable" bash -c 'test -x scripts/e2e_smoke.py'

if (( errors > 0 )); then
  printf '%d acceptance check(s) failed\n' "$errors" >&2
  exit 1
fi
printf 'All v20.3.0 mechanical acceptance checks passed.\n'

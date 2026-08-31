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
check "e2e rejects example credentials" bash -c 'grep -q "_is_placeholder" scripts/e2e_smoke.py && grep -q "YOUR_LLM_API_KEY" tests/test_v20_3_e2e_smoke.py'
check "e2e tenant has random suffix" bash -c 'grep -q "secrets.token_hex" scripts/e2e_smoke.py'
check "report.py exists and is executable" bash -c 'test -x scripts/report.py'
check "drill_autoshift contract exists" bash -c 'test -x scripts/drill_autoshift.sh && bash scripts/drill_autoshift.sh --check >/dev/null'
check "restore_gate exists and rejects invalid path" bash -c 'test -x scripts/restore_gate.sh && ! bash scripts/restore_gate.sh --dry-run /tmp/does-not-exist >/dev/null 2>&1'
check "crontab lists at least nine tasks" bash -c 'test "$(bash scripts/update_crontab.sh --list | python3 -c "import json,sys; print(len(json.load(sys.stdin)[\"tasks\"]))")" -ge 9'
check "one-line prompt is present and canonical" bash -c 'test -f prompts/install.txt && test -f ONE_LINE_INSTALL.md && cmp -s prompts/install.txt ONE_LINE_INSTALL.md && test "$(wc -l < prompts/install.txt)" -eq 1'
check "integration guide has canonical pointer" bash -c 'grep -q "Canonical contract" integrations/INTEGRATION_GUIDE.md && grep -q "docs/AGENT_INTEGRATION.md" integrations/INTEGRATION_GUIDE.md && ! grep -q "不做鉴权" integrations/INTEGRATION_GUIDE.md'
check "capacity and restore docs exist" bash -c 'test -f docs/CAPACITY.md && test -f docs/restore-comparison.md && grep -q "facts_watermark_effective" docs/CAPACITY.md && grep -q "restore_gate.sh" docs/restore-comparison.md'
check "README_EN preview/public-tag status" bash -c 'grep -q "private verification-line preview" README_EN.md && grep -q "public upstream Tag remains" README_EN.md'
check "dependency declarations match" bash -c 'test -x scripts/dependency_audit.py && python3 scripts/dependency_audit.py >/dev/null'
check "service units have memory limits" bash -c 'grep -q "MemoryHigh=768M" deploy/aidumem-api.service && grep -q "MemoryMax=1G" deploy/aidumem-api.service'
check "integration smoke script exists" bash -c 'test -x scripts/agent_integration_check.py && grep -q "/api/core-memory/inject" scripts/agent_integration_check.py'

if (( errors > 0 )); then
  printf '%d acceptance check(s) failed\n' "$errors" >&2
  exit 1
fi
printf 'All v20.3.0 mechanical acceptance checks passed.\n'

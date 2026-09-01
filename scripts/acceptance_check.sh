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
check "drill --run actually passes against a real health shape" bash -c 'test -f tests/test_v20_3_1_drill_autoshift.py && grep -q "test_drill_run_passes_against_real_health_shape" tests/test_v20_3_1_drill_autoshift.py'
check "restore_gate exists and rejects invalid path" bash -c 'test -x scripts/restore_gate.sh && ! bash scripts/restore_gate.sh --dry-run /tmp/does-not-exist >/dev/null 2>&1'
check "crontab intent list matches real TASKS array (8, ghost-free)" bash -c 'test "$(bash scripts/update_crontab.sh --list | python3 -c "import json,sys; print(len(json.load(sys.stdin)[\"tasks\"]))")" -eq 8'
check "crontab every task target script exists" bash -c 'bash scripts/update_crontab.sh --dry-run >/dev/null' 
check "deploy prompt is present and canonical" bash -c 'test -f prompts/install.txt && test -f ONE_LINE_INSTALL.md && cmp -s prompts/install.txt ONE_LINE_INSTALL.md && grep -q "report.py" prompts/install.txt && grep -q "e2e_smoke.py" prompts/install.txt && grep -q "agent_integration_check.py" prompts/install.txt && grep -q "update_crontab.sh" prompts/install.txt'
# v20.3.1（九份审计 P0-8 · 嘟嘟 🔴-4）：展示区与 canonical 的对账。
# 上一版 README/AGENTS 展示的是另一段旧文案却自称「唯一真源」——哈希对不上，
# 那句话就是假的。修复：展示区全部改为引用形态（不再复制正文），三处必须
# 同时满足「明确指向 install.txt 为 canon」且「不再宣称自己是真源」。
check "prompt display zones are reference-form, not divergent copies" bash -c '
  grep -q "prompts/install.txt" README.md &&
  grep -q "prompts/install.txt" README_EN.md &&
  grep -q "prompts/install.txt" AGENTS.md &&
  ! grep -q "唯一真源" README.md &&
  ! grep -q "唯一真源" README_EN.md &&
  ! grep -q "canonical source at" README_EN.md
'
check "integration guide has canonical pointer" bash -c 'grep -q "Canonical contract" integrations/INTEGRATION_GUIDE.md && grep -q "docs/AGENT_INTEGRATION.md" integrations/INTEGRATION_GUIDE.md && ! grep -q "不做鉴权" integrations/INTEGRATION_GUIDE.md'
check "capacity and restore docs exist" bash -c 'test -f docs/CAPACITY.md && test -f docs/restore-comparison.md && grep -q "facts_watermark_effective" docs/CAPACITY.md && grep -q "restore_gate.sh" docs/restore-comparison.md'
# v20.3.1（九份审计 P0-7 / GLM F-3.0）：上一版这行锚在叙事文案上（"private
# verification-line preview"），转正提交改了 README_EN 措辞 → 发布树上这项必红
# 而 Release 仍宣称 21/21。判据改锚**不变量**：两份 README 的版本串必须与
# version.py 的 SERVICE_VERSION 一致 —— 版本号变了守卫自动跟着走，不再依赖
# 有人记得改尺子。
VER="$(python3 -c 'import re;print(re.search(r"SERVICE_VERSION = \"([^\"]+)\"", open("ducky/version.py").read()).group(1))' 2>/dev/null || true)"
MAJOR_MINOR="$(printf '%s' "${VER}" | cut -d. -f1-2)"
check "README versions match version.py ($VER)" bash -c '
  test -n "'"$VER"'" &&
  grep -q "v'"${MAJOR_MINOR}"'" README.md &&
  grep -q "v'"${MAJOR_MINOR}"'" README_EN.md
'
check "dependency declarations match" bash -c 'test -x scripts/dependency_audit.py && python3 scripts/dependency_audit.py >/dev/null'
check "service units have memory limits" bash -c 'grep -q "MemoryHigh=768M" deploy/aidumem-api.service && grep -q "MemoryMax=1G" deploy/aidumem-api.service'
check "integration smoke script exists" bash -c 'test -x scripts/agent_integration_check.py && grep -q "/api/core-memory/inject" scripts/agent_integration_check.py'

# ── v20.3.1（九份审计 P0-7）：三道硬门槛 ──────────────────────────
# 21 项里约 15 项是文本存在性检查（自我指涉），此前缺的就是「真的执行」：
# 这三项直接跑工具并要求退出码 0。
check "hard gate: py_compile passes" bash -c '
  python3 -m py_compile $(git ls-files "*.py" | head -400) 2>/dev/null ||
  python3 - <<PYEOF
import compileall, sys
ok = compileall.compile_dir("ducky", quiet=2) and compileall.compile_file("api_server.py", quiet=2)
sys.exit(0 if ok else 1)
PYEOF
'
# 硬门槛的「真的执行」要量的是退出码，不是收集成功。全量跑给发布流程；
# 这里跑守卫自证子集（本轮新增测试 + 数字口径守卫），几分钟内出结果，
# 退出码 0 才算过 —— collect-only 会把「根本没跑」伪装成「跑了」。
PY="python3"
if [[ -x "${ROOT}/.venv/bin/python" ]]; then PY="${ROOT}/.venv/bin/python"; fi
if [[ -n "${AIDUMEM_PYTHON:-}" ]]; then PY="${AIDUMEM_PYTHON}"; fi
# ${PY} 作为位置参数传给 bash -c（"bash: /Users/jack/Documents/4.: No such
# file or directory" —— 路径含空格时把它拼进命令串会把解释器路径劈成两半）。
check "hard gate: pytest sentinel subset exits 0" bash -c '
  "$1" -m pytest tests/test_v20_3_1_gear_probe.py tests/test_v20_3_1_drill_autoshift.py \
    tests/test_v20_3_1_integration_check.py tests/test_v20_3_1_idempotency_paths.py \
    tests/test_first_run_experience.py -q >/dev/null 2>&1
' _ "${PY}"
check "hard gate: push_gate exits 0" bash -c 'test -x scripts/push_gate.sh'

if (( errors > 0 )); then
  printf '%d acceptance check(s) failed\n' "$errors" >&2
  exit 1
fi
printf 'All %s mechanical acceptance checks passed.\n' "${VER:-20.3.1}"

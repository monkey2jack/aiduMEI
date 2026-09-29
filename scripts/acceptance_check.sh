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

# f0.3（B6）：只 grep/统计文档文本、不执行任何行为的检查一律带 `DOC-` 前缀 ——
# 「N 项全过」里有多少是文档存在性，读结论的人应当一眼看得出来。
check "DOC-entry files exist" bash -c '
  test -f AGENTS.md &&
  test -f llms.txt &&
  test -f TROUBLESHOOTING.md &&
  test -f docs/OPERATIONS.md &&
  test -f docs/HEALTH.md &&
  test -f docs/AGENT_INTEGRATION.md &&
  test -f docs/BACKUP_RESTORE.md &&
  test -f scripts/README.md
'
check "DOC-both README line counts <=600" bash -c '
  test "$(wc -l < README.md)" -le 600 &&
  test "$(wc -l < README_EN.md)" -le 600
'
check "DOC-AGENTS.md <=12KB" bash -c 'test "$(wc -c < AGENTS.md)" -le 12000'
check "DOC-rerank example is nested" grep -q '"config": {' mem0_config_local.json.example
check "no hardcoded restore date" bash -c '! grep -q 20260727 scripts/restore_backup.py'
check "no hardcoded green probes" bash -c '! grep -q "\"injection_guard_ok\": True" ducky/hot/health.py && ! grep -q "\"port_service\": True" ducky/hot/health.py'
check "DOC-MCP port is 8766 in README" bash -c '! grep -q ":8768" README.md && ! grep -q ":8768" README_EN.md'
check "e2e script is executable" bash -c 'test -x scripts/e2e_smoke.py'
check "e2e rejects example credentials" bash -c 'grep -q "_is_placeholder" scripts/e2e_smoke.py && grep -q "YOUR_LLM_API_KEY" tests/test_v20_3_e2e_smoke.py'
check "e2e tenant has random suffix" bash -c 'grep -q "secrets.token_hex" scripts/e2e_smoke.py'
check "report.py exists and is executable" bash -c 'test -x scripts/report.py'
check "drill_autoshift contract exists" bash -c 'test -x scripts/drill_autoshift.sh && bash scripts/drill_autoshift.sh --check >/dev/null'
check "drill --run actually passes against a real health shape" bash -c 'test -f tests/test_v20_3_1_drill_autoshift.py && grep -q "test_drill_run_passes_against_real_health_shape" tests/test_v20_3_1_drill_autoshift.py'
check "restore_gate exists and rejects invalid path" bash -c 'test -x scripts/restore_gate.sh && ! bash scripts/restore_gate.sh --dry-run /tmp/does-not-exist >/dev/null 2>&1'
# f0.3（B6）：此前这项拿 TASKS 声明数去比同一数组的 --list 输出 —— 同一份
# 数据数两遍，恒等式，永远绿。现在对账**世界**：本机有 crontab 且其中有本
# checkout 的条目时，update_crontab.sh --installed 必须报 ok 且实装数 == 清单数
# （它也会把清单外跑本仓脚本的行判为漂移）；没有 crontab、或 crontab 里只有
# 别的 checkout / 别的任务时，明确打印 SKIP —— SKIP 不计入 PASS。
# >>> gate-cron-installed
if crontab -l >/dev/null 2>&1; then
  CRON_VERDICT="$(bash scripts/update_crontab.sh --installed 2>/dev/null | python3 -c '
import json, sys
try:
    report = json.load(sys.stdin)
except ValueError:
    print("unreadable"); sys.exit(0)
tasks = report.get("tasks") or {}
owned = [t for t in tasks.values() if t.get("status") != "missing"]
if not owned and not report.get("unexpected") and not report.get("unmanaged"):
    print("none")
else:
    good = report.get("ok") is True and report.get("installed") == report.get("expected")
    print("%s %s/%s" % ("ok" if good else "bad", report.get("installed"), report.get("expected")))
' || true)"
  if [[ "${CRON_VERDICT}" == "none" ]]; then
    printf 'SKIP cron installed-state: this crontab holds no aiduMEI entries for %s (not a maintenance host for this checkout)\n' "${ROOT}"
  else
    check "cron installed-state matches the manifest (update_crontab.sh --installed: ${CRON_VERDICT#* })" test "${CRON_VERDICT%% *}" = ok
  fi
else
  printf 'SKIP cron installed-state: no crontab for this user on this host\n'
fi
# <<< gate-cron-installed
check "DOC-canonical install prompt cron count matches actual manifest" bash -c 'listed=$(bash scripts/update_crontab.sh --list | python3 -c "import json,sys; print(len(json.load(sys.stdin)[\"tasks\"]))"); test "$listed" -gt 0 && grep -q "安装 $listed 项定时任务" prompts/install.txt && grep -q "实文 $listed 条" prompts/install.txt && cmp -s prompts/install.txt ONE_LINE_INSTALL.md'
check "crontab every task target script exists" bash -c 'bash scripts/update_crontab.sh --dry-run >/dev/null' 
check "DOC-deploy prompt is present and canonical" bash -c 'test -f prompts/install.txt && test -f ONE_LINE_INSTALL.md && cmp -s prompts/install.txt ONE_LINE_INSTALL.md && grep -q "report.py" prompts/install.txt && grep -q "e2e_smoke.py" prompts/install.txt && grep -q "agent_integration_check.py" prompts/install.txt && grep -q "update_crontab.sh" prompts/install.txt'
# v20.3.1（九份审计 P0-8 · 用户审计 🔴-4）：展示区与 canonical 的对账。
# 上一版 README/AGENTS 展示的是另一段旧文案却自称「唯一真源」——哈希对不上，
# 那句话就是假的。修复：展示区全部改为引用形态（不再复制正文），三处必须
# 同时满足「明确指向 install.txt 为 canon」且「不再宣称自己是真源」。
check "DOC-prompt display zones are reference-form, not divergent copies" bash -c '
  grep -q "prompts/install.txt" README.md &&
  grep -q "prompts/install.txt" README_EN.md &&
  grep -q "prompts/install.txt" AGENTS.md &&
  ! grep -q "唯一真源" README.md &&
  ! grep -q "唯一真源" README_EN.md &&
  ! grep -q "canonical source at" README_EN.md
'
check "DOC-integration guide has canonical pointer" bash -c 'grep -q "Canonical contract" integrations/INTEGRATION_GUIDE.md && grep -q "docs/AGENT_INTEGRATION.md" integrations/INTEGRATION_GUIDE.md && ! grep -q "不做鉴权" integrations/INTEGRATION_GUIDE.md'
check "DOC-capacity and restore docs exist" bash -c 'test -f docs/CAPACITY.md && test -f docs/restore-comparison.md && grep -q "facts_watermark_effective" docs/CAPACITY.md && grep -q "restore_gate.sh" docs/restore-comparison.md'
# v20.3.1（九份审计 P0-7 / GLM F-3.0）：上一版这行锚在叙事文案上（"private
# verification-line preview"），转正提交改了 README_EN 措辞 → 发布树上这项必红
# 而 Release 仍宣称 21/21。判据改锚**不变量**：两份 README 的版本串必须与
# version.py 的 SERVICE_VERSION 一致 —— 版本号变了守卫自动跟着走，不再依赖
# 有人记得改尺子。
VER="$(python3 -c 'import re;print(re.search(r"SERVICE_VERSION = \"([^\"]+)\"", open("ducky/version.py").read()).group(1))' 2>/dev/null || true)"
MAJOR_MINOR="$(printf '%s' "${VER}" | cut -d. -f1-2)"
PUBLIC_VER="$(python3 -c 'import re; m=re.search(r"^FULL_VERSION = \"([^\"]+)\"", open("ducky/version.py").read(), re.M); print(m.group(1) if m else "")' 2>/dev/null || true)"
if [[ -z "${PUBLIC_VER}" ]]; then PUBLIC_VER="v${MAJOR_MINOR}"; fi
check "DOC-README versions match version.py (${PUBLIC_VER} / ${VER})" bash -c '
  test -n "$1" && test -n "$2" &&
  head -n 20 README.md | grep -Fq "当前公开版本 $2" &&
  head -n 20 README_EN.md | grep -Fq "current public release is **$2**"
' _ "${VER}" "${PUBLIC_VER}"
check "dependency declarations match" bash -c 'test -x scripts/dependency_audit.py && python3 scripts/dependency_audit.py >/dev/null'
check "service units have memory limits" bash -c 'grep -q "MemoryHigh=768M" deploy/aidumem-api.service && grep -q "MemoryMax=1G" deploy/aidumem-api.service'
check "integration smoke script exists" bash -c 'test -x scripts/agent_integration_check.py && grep -q "/api/core-memory/inject" scripts/agent_integration_check.py'

# ── v20.3.1（九份审计 P0-7）：三道硬门槛 ──────────────────────────
# 21 项里约 15 项是文本存在性检查（自我指涉），此前缺的就是「真的执行」：
# 这三项直接跑工具并要求退出码 0。
# 探测链与 update_crontab.sh 同款：显式指定 > 仓内 .venv > 仓内 venv（f0.3：
# 生产部署树用的是不带点的 venv/，此前只认 .venv → 「no interpreter with pytest
# found」）> PATH 上能 import pytest 的 python3。仓内两个 venv 只在能 import
# pytest 时才入选，缺 pytest 的部署 venv 不挡住后面的候选。
# 沙箱形态（用部署 venv 跑测试、树内无 .venv）此前掉进系统 python3（无 pytest）
# → 哨兵子集红 —— 环境轴，不是产品缺陷。py_compile 也用同一个解释器：语法
# 是否合法取决于 Python 版本，门禁结论必须与其余硬门槛同一口径。
# >>> gate-interpreter
PY="${AIDUMEM_PYTHON:-}"
if [[ -z "${PY}" ]]; then
  for cand in "${ROOT}/.venv/bin/python" "${ROOT}/venv/bin/python"; do
    if [[ -x "${cand}" ]] && "${cand}" -c "import pytest" >/dev/null 2>&1; then
      PY="${cand}"; break
    fi
  done
fi
if [[ -z "${PY}" ]]; then
  for cand in python3 python3.12 python3.11 python3.10; do
    if command -v "${cand}" >/dev/null 2>&1 && "${cand}" -c "import pytest" >/dev/null 2>&1; then
      PY="${cand}"; break
    fi
  done
fi
# <<< gate-interpreter
if [[ -z "${PY}" ]]; then
  printf 'FAIL hard gate: no interpreter with pytest found (set AIDUMEM_PYTHON)\n' >&2
  exit 1
fi
# v20.4.0（三方审计 P0-3 · 动态审计 🔴-3）：门禁结论必须带解释器口径。
# 「acceptance 全过」离开「在哪个解释器、装了哪些可选轴」就不可复现 ——
# 动态审计换两种解释器各红一次，而报告只写了结论没写口径。自报家门：
printf 'GATE interpreter: %s (%s)\n' "${PY}" "$("${PY}" -c 'import sys; print(sys.version.split()[0], "@", sys.prefix)')"
if ! "${PY}" -c "import pytest" >/dev/null 2>&1; then
  printf 'FAIL hard gate: AIDUMEM_PYTHON=%s has no pytest — gate verdicts from this interpreter are void\n' "${PY}" >&2
  exit 1
fi
# f0.3（B6）：此前是 `py_compile $(git ls-files "*.py" | head -400) 2>/dev/null`
# —— 前 400 个之外的文件从不编译，报错被丢进黑洞，失败再退回只编 ducky/ 的
# compileall。现在：编译**每一个** git 跟踪的 .py（compile()，不往树里写 .pyc），
# 编译成功数必须 == `git ls-files '*.py'` 的条数且 > 0，失败逐个列出。
# >>> gate-py-compile
if PYC_OUT="$("${PY}" -c '
import subprocess, sys, tokenize
try:
    listed = subprocess.run(["git", "ls-files", "-z", "--", "*.py"], check=True,
                            capture_output=True).stdout
except (OSError, subprocess.CalledProcessError) as exc:
    print("cannot enumerate tracked .py files (not a git checkout?): %s" % exc)
    sys.exit(1)
files = [name for name in listed.decode("utf-8", "surrogateescape").split("\0") if name]
compiled = 0
for path in files:
    try:
        with tokenize.open(path) as handle:
            compile(handle.read(), path, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, OSError) as exc:
        print("does not compile: %s: %s" % (path, exc))
        continue
    compiled += 1
print("%d/%d git-tracked .py files compile" % (compiled, len(files)))
sys.exit(0 if files and compiled == len(files) else 1)
' 2>&1)"; then
  printf 'PASS hard gate: py_compile passes (%s)\n' "${PYC_OUT}"
else
  printf 'FAIL hard gate: py_compile passes\n%s\n' "${PYC_OUT}" >&2
  errors=$((errors+1))
fi
# <<< gate-py-compile
# 硬门槛的「真的执行」要量的是退出码，不是收集成功。全量跑给发布流程；
# 这里跑守卫自证子集（本轮新增测试 + 数字口径守卫），几分钟内出结果，
# 退出码 0 才算过 —— collect-only 会把「根本没跑」伪装成「跑了」。
# ${PY} 作为位置参数传给 bash -c（"bash: <含空格路径的前半段>: No such
# file or directory" —— 路径含空格时把它拼进命令串会把解释器路径劈成两半）。
check "hard gate: pytest sentinel subset exits 0" bash -c '
  "$1" -m pytest tests/test_v20_3_1_gear_probe.py tests/test_v20_3_1_drill_autoshift.py \
    tests/test_v20_3_1_integration_check.py tests/test_v20_3_1_idempotency_paths.py \
    tests/test_first_run_experience.py -q >/dev/null 2>&1
' _ "${PY}"
# v20.3.2 正式版（用户审计 C）：用例名说「exits 0」，判据原先只测 `test -x` —— 与 P2-16
# 同型，同一个文件里。27 项全 PASS 有一项是这么 PASS 的。现在真跑。
check "hard gate: push_gate exits 0" env AIDUMEM_PYTHON="${PY}" bash scripts/push_gate.sh

if (( errors > 0 )); then
  printf '%d acceptance check(s) failed\n' "$errors" >&2
  exit 1
fi
printf 'All %s mechanical acceptance checks passed.\n' "${VER:-20.3.1}"

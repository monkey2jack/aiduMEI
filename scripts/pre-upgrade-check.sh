#!/bin/bash
# ============================================================================
# aiduMEM 升级前验证脚本
# 2026-06-14
#
# 用途: 在升级 mem0ai / qdrant-client / fastapi 之前跑这 5 步，确认基线干净
# 调用: bash scripts/pre-upgrade-check.sh
# 退出码: 0 = 全过；1 = 有失败
#
# v19.4.0（生产审计 🟡-B）：备份纪律接进升级入口——
#   · 步骤 1 的备份改走 backup_gate.sh create（自带 sha256 + quick_check
#     + .backup_verified 标记，备份目录命名 pre-<label>-<ts>）
#   · 新增步骤 2 硬门禁 backup_gate.sh require：无已验证备份则 exit 1，
#     升级不许开始（B2 备份纪律从「有脚本」变成「卡入口」）
# ============================================================================

set -euo pipefail

# --- 路径常量 ---------------------------------------------------------------
# 仓库根自动解析（本文件位于 <repo>/scripts/），可用 AIDUMEM_HOME 覆盖
REPO_ROOT="${AIDUMEM_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
API_BASE="${AIDUMEM_API_BASE:-http://127.0.0.1:8767}"
BACKUP_ROOT="${AIDUMEM_BACKUP_ROOT:-$(dirname "${REPO_ROOT}")}"
SCRIPTS_DIR="${REPO_ROOT}/scripts"
TESTS_DIR="${REPO_ROOT}/tests"

# --- 统计 --------------------------------------------------------------------
PASS=0
FAIL=0
declare -a RESULTS

# 颜色（如果终端支持）
if [[ -t 1 ]]; then
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[0;33m'
  RESET='\033[0m'
else
  GREEN=''; RED=''; YELLOW=''; RESET=''
fi

step() { echo ""; echo "════════════════════════════════════════"; echo "🔧 $1"; echo "════════════════════════════════════════"; }
ok()   { echo -e "  ${GREEN}✅ PASS${RESET} $1"; PASS=$((PASS+1)); RESULTS+=("✅ $1"); }
bad()  { echo -e "  ${RED}❌ FAIL${RESET} $1"; FAIL=$((FAIL+1)); RESULTS+=("❌ $1"); }
warn() { echo -e "  ${YELLOW}⚠️  WARN${RESET} $1"; RESULTS+=("⚠️  $1"); }

# ============================================================================
# 步骤 1: 备份现状（走 backup_gate：sha256 + quick_check + 验证标记）
# ============================================================================
step "步骤 1/5 — 备份现状（backup_gate create）"

BACKUP_DIR=""

if [[ ! -d "${REPO_ROOT}" ]]; then
  bad "源目录不存在: ${REPO_ROOT}（异常，请检查）"
  exit 1
fi

# 备份数据目录（生产数据安全第一）；代码仓备份在门禁之后轻量补一份
GATE_DATA_DIR="${AIDUMEM_DATA_DIR:-${REPO_ROOT}/data}"
echo "  📦 备份数据目录 ${GATE_DATA_DIR}（backup_gate.sh create pre-upgrade）"
if GATE_OUT=$(AIDUMEM_BACKUP_ROOT="${BACKUP_ROOT}" AIDUMEM_DATA_DIR="${GATE_DATA_DIR}" \
        bash "${SCRIPTS_DIR}/backup_gate.sh" create pre-upgrade 2>&1); then
  BACKUP_DIR=$(printf '%s\n' "${GATE_OUT}" | tail -1)
  SIZE=$(du -sh "${BACKUP_DIR}" 2>/dev/null | cut -f1)
  ok "数据备份完成（sha256 + quick_check 已过）: ${BACKUP_DIR} (${SIZE})"
else
  bad "backup_gate 备份失败: $(printf '%s' "${GATE_OUT}" | tail -1)"
fi

# 代码仓轻量备份（排除 venv / 缓存 / 大包，保持轻量；不进门禁链）
TS="$(date +%Y%m%d_%H%M%S)"
CODE_BACKUP_DIR="${BACKUP_ROOT}/aidumem.bak-pre-upgrade-${TS}"
echo "  📦 备份代码仓 ${REPO_ROOT} → ${CODE_BACKUP_DIR}"
if cp -a --exclude='venv' --exclude='__pycache__' --exclude='*.tar.gz' \
        --exclude='data.bak-*' --exclude='*.bak-*' \
        "${REPO_ROOT}" "${CODE_BACKUP_DIR}" 2>/dev/null; then
  ok "代码仓备份完成: ${CODE_BACKUP_DIR}"
else
  bad "代码仓备份失败（cp 异常）"
fi

# ============================================================================
# 步骤 2: 备份硬门禁（backup_gate require）
# ============================================================================
step "步骤 2/5 — 备份硬门禁（backup_gate require）"

if AIDUMEM_BACKUP_ROOT="${BACKUP_ROOT}" bash "${SCRIPTS_DIR}/backup_gate.sh" require; then
  ok "硬门禁放行：存在通过 sha256 验证的迁移前备份"
else
  bad "硬门禁拦截：无已验证备份，升级不许开始（先修步骤 1）"
fi

# ============================================================================
# 步骤 3: 5 个端点 smoke test
# ============================================================================
step "步骤 3/5 — API 端点 smoke test (5 个)"

smoke() {
  local name="$1"
  local method="$2"
  local path="$3"
  local extra="${4:-}"
  local t0 t1 ms
  t0=$(date +%s%3N)
  local code
  if [[ "${method}" == "GET" ]]; then
    code=$(curl -s -o /tmp/pre_upg_body -w "%{http_code}" "${API_BASE}${path}" || echo "000")
  else
    code=$(curl -s -o /tmp/pre_upg_body -w "%{http_code}" -X "${method}" \
              -H "Content-Type: application/json" -d "${extra}" \
              "${API_BASE}${path}" || echo "000")
  fi
  t1=$(date +%s%3N); ms=$((t1 - t0))
  if [[ "${code}" =~ ^2 ]]; then
    ok "${method} ${path} → ${code} (${ms}ms)"
  else
    bad "${method} ${path} → ${code} (${ms}ms)"
  fi
}

smoke "health"     GET  "/health"
smoke "stats"      GET  "/stats"
smoke "categories" GET  "/facts/categories"
smoke "list user"  GET  "/facts?category=%E5%A4%A7%E5%8F%94"
smoke "search user" GET  "/facts/search?query=%E5%A4%A7%E5%8F%94&top_k=5"

# ============================================================================
# 步骤 4: 3 个 cron 脚本的 --dry-run
# ============================================================================
step "步骤 4/5 — 3 个 cron 脚本 --dry-run"

dry_run() {
  local script="$1"
  local path="${SCRIPTS_DIR}/${script}"
  if [[ ! -f "${path}" ]]; then
    warn "脚本不存在，跳过: ${script}"
    return
  fi
  # 先看脚本是否声明 --dry-run
  if grep -q -- "--dry-run" "${path}" 2>/dev/null; then
    echo "  🧪 ${script} --dry-run"
    if python3 "${path}" --dry-run 2>&1 | tail -5; then
      ok "${script} --dry-run 完成"
    else
      bad "${script} --dry-run 失败"
    fi
  else
    warn "${script} 不支持 --dry-run（已跳过）"
  fi
}

dry_run "dedup_facts.py"
dry_run "decay_scanner.py"
dry_run "recompute_trust.py"

# ============================================================================
# 步骤 5: 端到端集成测试
# ============================================================================
step "步骤 5/5 — 端到端集成测试"

E2E_TEST="${TESTS_DIR}/test_e2e_smoke.py"
if [[ -f "${E2E_TEST}" ]]; then
  echo "  🧪 ${E2E_TEST}"
  if python3 "${E2E_TEST}" 2>&1 | tail -20; then
    ok "test_e2e_smoke.py 跑完"
  else
    bad "test_e2e_smoke.py 失败"
  fi
else
  warn "TODO: ${E2E_TEST} 不存在，跳过端到端测试（建议补上）"
fi

# ============================================================================
# 摘要
# ============================================================================
echo ""
echo "════════════════════════════════════════"
echo "📊 升级前验证摘要"
echo "════════════════════════════════════════"
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo ""
echo -e "  ${GREEN}通过: ${PASS}${RESET}  |  ${RED}失败: ${FAIL}${RESET}"
echo "  📦 数据备份（进门禁链）: ${BACKUP_DIR:-无}"
echo "  📦 代码仓备份: ${CODE_BACKUP_DIR:-无}"
echo ""

if [[ "${FAIL}" -gt 0 ]]; then
  echo -e "${RED}❌ 有 ${FAIL} 项失败，升级前需先修复${RESET}"
  exit 1
else
  echo -e "${GREEN}✅ 全部通过，可以开始升级${RESET}"
  exit 0
fi

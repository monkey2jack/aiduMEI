#!/bin/bash
# ============================================================================
# aiduMEM 升级前验证脚本
# 2026-06-14
#
# 用途: 在升级 mem0ai / qdrant-client / fastapi 之前跑这 4 步，确认基线干净
# 调用: bash scripts/pre-upgrade-check.sh
# 退出码: 0 = 全过；1 = 有失败
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
# 步骤 1: 备份现状
# ============================================================================
step "步骤 1/4 — 备份现状"

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/aidumem.bak-pre-upgrade-${TS}"

if [[ ! -d "${REPO_ROOT}" ]]; then
  bad "源目录不存在: ${REPO_ROOT}（异常，请检查）"
  exit 1
fi

# 排除掉 wheels tar.gz / venv / data 这种大块头，保持备份轻量
echo "  📦 备份 ${REPO_ROOT} → ${BACKUP_DIR}"
if cp -a --exclude='venv' --exclude='__pycache__' --exclude='*.tar.gz' \
        --exclude='data.bak-*' --exclude='*.bak-*' \
        "${REPO_ROOT}" "${BACKUP_DIR}" 2>/dev/null; then
  SIZE=$(du -sh "${BACKUP_DIR}" 2>/dev/null | cut -f1)
  ok "备份完成: ${BACKUP_DIR} (${SIZE})"
else
  bad "备份失败（cp 异常）"
fi

# ============================================================================
# 步骤 2: 5 个端点 smoke test
# ============================================================================
step "步骤 2/4 — API 端点 smoke test (5 个)"

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
# 步骤 3: 3 个 cron 脚本的 --dry-run
# ============================================================================
step "步骤 3/4 — 3 个 cron 脚本 --dry-run"

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
# 步骤 4: 端到端集成测试
# ============================================================================
step "步骤 4/4 — 端到端集成测试"

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
echo "  📦 备份目录: ${BACKUP_DIR}"
echo ""

if [[ "${FAIL}" -gt 0 ]]; then
  echo -e "${RED}❌ 有 ${FAIL} 项失败，升级前需先修复${RESET}"
  exit 1
else
  echo -e "${GREEN}✅ 全部通过，可以开始升级${RESET}"
  exit 0
fi

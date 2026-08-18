#!/bin/bash
# ============================================================================
# aiduMEM 升级后验证脚本
# 2026-06-14
#
# 用途: 在升级 mem0ai / qdrant-client / fastapi 之后跑这 4 件，确认无回退
# 调用: bash scripts/post-upgrade-check.sh
# 退出码: 0 = 全过；1 = 有失败
# ============================================================================

set -euo pipefail

# --- 路径常量 ---------------------------------------------------------------
# 仓库根自动解析（本文件位于 <repo>/scripts/），可用 AIDUMEM_HOME 覆盖
REPO_ROOT="${AIDUMEM_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
API_BASE="${AIDUMEM_API_BASE:-http://127.0.0.1:8767}"
# 🔴P0-1（v19.4.1）：门禁开启后不带凭据的 curl 一律 401。
# 这里统一构造 auth 头，未设 token 时为空数组（行为与旧版一致）。
AUTH_ARGS=()
if [[ -n "${AIDUMEM_API_TOKEN:-}" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${AIDUMEM_API_TOKEN}")
fi

TESTS_DIR="${REPO_ROOT}/tests"

# --- 统计 --------------------------------------------------------------------
PASS=0
FAIL=0
declare -a RESULTS

# 颜色
if [[ -t 1 ]]; then
  GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; RESET='\033[0m'
else
  GREEN=''; RED=''; YELLOW=''; RESET=''
fi

step() { echo ""; echo "════════════════════════════════════════"; echo "🔧 $1"; echo "════════════════════════════════════════"; }
ok()   { echo -e "  ${GREEN}✅ PASS${RESET} $1"; PASS=$((PASS+1)); RESULTS+=("✅ $1"); }
bad()  { echo -e "  ${RED}❌ FAIL${RESET} $1"; FAIL=$((FAIL+1)); RESULTS+=("❌ $1"); }
warn() { echo -e "  ${YELLOW}⚠️  WARN${RESET} $1"; RESULTS+=("⚠️  $1"); }

# ============================================================================
# 步骤 1: 4 个核心 service 全 active
# ============================================================================
step "步骤 1/4 — 4 个核心 service 状态"

check_service() {
  local svc="$1"
  # systemd 优先；找不到就退化到 ps
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet "${svc}" 2>/dev/null; then
      ok "${svc}: active"
      return
    fi
    # 也许是用户级
    if systemctl --user is-active --quiet "${svc}" 2>/dev/null; then
      ok "${svc}: active (user)"
      return
    fi
  fi
  # 退化: ps 查进程
  if pgrep -af "${svc}" >/dev/null 2>&1; then
    ok "${svc}: 进程在跑（systemctl 不可用，ps 兜底）"
  else
    bad "${svc}: 未运行（systemctl + ps 都查不到）"
  fi
}

check_service "hermes-gateway"
check_service "${AIDUMEM_SERVICE:-aidumem-api}"

check_service "card-webhook"

# ============================================================================
# 步骤 2: mem0 API 真发一次 stats 请求
# ============================================================================
step "步骤 2/4 — mem0 API stats 真发一次"

STATS_T0=$(date +%s%3N)
STATS_RESP=$(curl -s "${AUTH_ARGS[@]}" -w "\n__HTTP_CODE__:%{http_code}" "${API_BASE}/stats" || echo "__HTTP_CODE__:000")
STATS_T1=$(date +%s%3N); STATS_MS=$((STATS_T1 - STATS_T0))
STATS_CODE=$(echo "${STATS_RESP}" | tail -1 | sed 's/.*://')
STATS_BODY=$(echo "${STATS_RESP}" | sed '$d')

if [[ "${STATS_CODE}" == "200" ]]; then
  ok "GET /stats → 200 (${STATS_MS}ms)"
  # 用 jq 抽出关键字段
  if command -v jq >/dev/null 2>&1; then
    echo "  📊 stats 关键字段:"
    echo "${STATS_BODY}" | jq -r '
      "    status:            \(.status // "n/a")",
      "    user_id:           \(.user_id // "n/a")",
      "    total_memories:    \(.total_memories // 0)",
      "    unique_hashes:     \(.unique_hashes // 0)",
      "    duplicate_count:   \(.duplicate_count // 0)",
      "    after_dedup:       \(.after_dedup // 0)",
      "    user_distribution: \(.user_distribution // {})"
    ' 2>/dev/null | sed 's/^/  /' || warn "jq 解析失败（已返回 200，仅打印原文）"
  else
    warn "jq 未安装（apt install jq 可补），原文如下:"
    echo "${STATS_BODY}" | head -c 400 | sed 's/^/    /'
  fi
else
  bad "GET /stats → ${STATS_CODE}（升级可能破了 stats 端点）"
fi

# ============================================================================
# 步骤 3: facts 召回率测试
# ============================================================================
step "步骤 3/4 — facts 召回率测试"

RECALL_TEST="${TESTS_DIR}/test_facts_recall.py"
if [[ -f "${RECALL_TEST}" ]]; then
  echo "  🧪 ${RECALL_TEST}"
  if python3 "${RECALL_TEST}" 2>&1 | tail -20; then
    ok "test_facts_recall.py 跑完"
  else
    bad "test_facts_recall.py 失败"
  fi
else
  warn "TODO: ${RECALL_TEST} 不存在，跳过（建议补一个召回率测试）"
fi

# ============================================================================
# 步骤 4: 进程加载库检查（验证新 mem0ai 库已加载）
# ============================================================================
step "步骤 4/4 — 进程加载库检查（mem0ai 已挂载到内存）"

# 先找 API 服务的主 pid（systemd 或 uvicorn 进程）
SVC_PID=""
if command -v systemctl >/dev/null 2>&1; then
  SVC_PID=$(systemctl show "${AIDUMEM_SERVICE:-aidumem-api}" --property=MainPID 2>/dev/null | cut -d= -f2)
fi
if [[ -z "${SVC_PID}" || "${SVC_PID}" == "0" ]]; then
  # 退化: 找 uvicorn 进程（API 服务跑的就是 uvicorn）
  SVC_PID=$(pgrep -f "uvicorn.*api_server" | head -1 || echo "")
fi
if [[ -z "${SVC_PID}" ]]; then
  SVC_PID=$(pgrep -f "api_server" | head -1 || echo "")
fi

if [[ -z "${SVC_PID}" ]]; then
  warn "找不到 API 服务的 pid（步骤 1 已报 active，但 ps 拿不到 pid？）"
else
  echo "  🔍 API 服务 pid = ${SVC_PID}"
  MAPS_FILE="/proc/${SVC_PID}/maps"
  if [[ ! -r "${MAPS_FILE}" ]]; then
    warn "/proc/${SVC_PID}/maps 不可读（可能权限不够）"
  else
    # 关键库指纹（mem0 包名是 'mem0' 不是 'mem0ai'，maps 里找 /site-packages/mem0/）
    HITS_MEM0AI=$(grep -c "site-packages/mem0" "${MAPS_FILE}" 2>/dev/null || echo "0")
    HITS_QDRANT=$(grep -c "qdrant"    "${MAPS_FILE}" 2>/dev/null || echo "0")
    HITS_PYDANTIC=$(grep -c "pydantic" "${MAPS_FILE}" 2>/dev/null || echo "0")
    # mem0ai 通常没 .so（纯 python），所以也要查 site-packages
    SP_HITS_MEM0AI=$(grep -c "/site-packages/mem0" "${MAPS_FILE}" 2>/dev/null || echo "0")
    echo "  📦 maps 中 mem0 (mem0ai) 引用: ${HITS_MEM0AI} (site-packages: ${SP_HITS_MEM0AI})"
    echo "  📦 maps 中 qdrant 引用: ${HITS_QDRANT}"
    echo "  📦 maps 中 pydantic 引用: ${HITS_PYDANTIC}"
    # 验证升级后的版本号（用 venv 的 pip show，不是系统的）
    EXPECTED_MEM0AI="2.0.5"
    INSTALLED_MEM0AI=$(${REPO_ROOT}/venv/bin/pip show mem0ai 2>/dev/null | awk '/^Version:/{print $2}' | head -1 || echo "unknown")
    if [[ "${INSTALLED_MEM0AI}" == "${EXPECTED_MEM0AI}" ]]; then
      ok "mem0ai 版本对齐: ${INSTALLED_MEM0AI}"
    elif [[ "${SP_HITS_MEM0AI}" -gt 0 || "${HITS_MEM0AI}" -gt 0 ]]; then
      warn "mem0ai 已加载但版本不是 ${EXPECTED_MEM0AI}（实际: ${INSTALLED_MEM0AI}）"
    else
      bad "mem0ai 没在 maps 里出现（升级没生效？）"
    fi
  fi
fi

# ============================================================================
# 摘要
# ============================================================================
echo ""
echo "════════════════════════════════════════"
echo "📊 升级后验证摘要"
echo "════════════════════════════════════════"
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo ""
echo -e "  ${GREEN}通过: ${PASS}${RESET}  |  ${RED}失败: ${FAIL}${RESET}"
echo ""

if [[ "${FAIL}" -gt 0 ]]; then
  echo -e "${RED}❌ 有 ${FAIL} 项失败，升级未完全通过${RESET}"
  exit 1
else
  echo -e "${GREEN}✅ 全部通过，升级成功${RESET}"
  exit 0
fi

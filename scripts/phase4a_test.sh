#!/bin/bash
# Phase 4A 注入 API 测试脚本
# 5 个用例 + 摘要

set -u
URL="${AIDUMEM_API_BASE:-http://127.0.0.1:8767}/api/mem0/inject-context"

# 🔴P0-1（v19.4.1）：门禁开启后不带凭据的 curl 一律 401。
# 未设 token 时为空数组（行为与旧版一致）。
AUTH_ARGS=()
if [[ -n "${AIDUMEM_API_TOKEN:-}" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${AIDUMEM_API_TOKEN}")
fi
PASS=0
FAIL=0
declare -a RESULTS

run_test() {
  local name="$1"
  local query="$2"
  local k="${3:-5}"
  local expect_keywords="$4"  # 期望的类别关键字（用 / 分隔）

  echo ""
  echo "────────────────────────────────────────"
  echo "🧪 测试: $name"
  echo "   query: $query"
  echo "   k: $k | 期望含类别: $expect_keywords"
  echo "────────────────────────────────────────"
  local resp
  resp=$(curl -s "${AUTH_ARGS[@]}" -X POST "$URL" \
    -H "Content-Type: application/json" \
    -d "{\"query\": \"$query\", \"k\": $k}")
  if [ $? -ne 0 ]; then
    echo "❌ curl 失败"
    FAIL=$((FAIL+1))
    return
  fi
  # 提取 context_block 看看
  echo "$resp" | python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
print('--- context_block ---')
print(d.get('context_block', '(空)'))
print('--- facts_used ---')
for f in d.get('facts_used', []):
    print(f\"  - id={f['id']:3d} cat={f['category']:5s} key={f['fact_key']:8s} sim={f['sim']:.3f} score={f['score']:.3f} trust={f['trust_score']:.2f}\")
print(f'--- 统计 ---')
print(f\"  total_tokens: {d.get('total_tokens')}\")
print(f\"  candidates_total: {d.get('candidates_total')}\")
print(f\"  candidates_after_dedup: {d.get('candidates_after_dedup')}\")
" 2>&1
  # 检查期望类别
  if echo "$resp" | grep -q "$expect_keywords"; then
    echo "✅ PASS (含期望类别: $expect_keywords)"
    PASS=$((PASS+1))
  else
    echo "❌ FAIL (未含期望类别: $expect_keywords)"
    FAIL=$((FAIL+1))
  fi
}

run_test "1-个人档案"    "用户的生日是什么" 3 "user"
run_test "2-时间线"      "我们是怎么认识的" 5 "相遇"
run_test "3-mem0升级"    "mem0 怎么升级" 5 "user|general|AI"  # 升级相关信息可能没专门的 fact，应返回低置信度或不相关
run_test "4-闲聊天气"    "今天天气不错" 5 "general"  # 期望返回空
run_test "5-约定口令"    "常用口令" 5 "暗号"

echo ""
echo "════════════════════════════════════════"
echo "📊 测试总结: $PASS 通过 / $FAIL 失败"
echo "════════════════════════════════════════"

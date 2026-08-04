#!/usr/bin/env bash
# aidumem-inject.sh — Hermes Agent `pre_llm_call` shell hook
# =====================================================================
# 把 aiduMEM 的三层记忆注入到下一轮 LLM 请求：
#   1. CoreMemory   — 常驻结构化 block（身份 / 当前项目 / 关键决策）
#   2. Checkpoint   — 仅新会话首轮，续上上次断点
#   3. /search      — 按当轮 user_message 做相关性检索
#
# stdin  : Hermes pre_llm_call payload (JSON)
# stdout : {"context": "..."} 或 {} （无内容时静默）
#
# 配置（全部可选，均有默认值）：
#   AIDUMEM_URL             服务地址，默认 http://127.0.0.1:8767
#   AIDUMEM_USER_ID         记忆归属用户，默认 default
#   AIDUMEM_MIN_HISTORY     少于 N 条历史不注入检索，默认 4
#   AIDUMEM_NEW_SESSION_MAX 历史 ≤ N 条视为新会话（注入 checkpoint），默认 8
#   AIDUMEM_SEARCH_LIMIT    检索条数，默认 5
#   AIDUMEM_TIMEOUT         单次 HTTP 超时秒数，默认 2.0
#
# 安装：
#   cp integrations/aidumem-inject.sh ~/.hermes/agent-hooks/
#   chmod +x ~/.hermes/agent-hooks/aidumem-inject.sh
#   # 然后在 ~/.hermes/config.yaml 注册 hooks.pre_llm_call
#   详见 integrations/INTEGRATION_GUIDE.md
#
# 设计原则：
#   - 绝不影响 LLM 调用：硬超时 + 任何异常输出 {} 并 exit 0
#   - 短消息 / 短会话不注入，省 token
#   - 0 结果不注入，避免污染 context

set -uo pipefail

# export 是必须的：下面所有 python3 子进程都通过 os.environ 读这些值
export AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}"
export AIDUMEM_USER_ID="${AIDUMEM_USER_ID:-default}"
export AIDUMEM_SEARCH_LIMIT="${AIDUMEM_SEARCH_LIMIT:-5}"
export AIDUMEM_TIMEOUT="${AIDUMEM_TIMEOUT:-2.0}"
AIDUMEM_MIN_HISTORY="${AIDUMEM_MIN_HISTORY:-4}"
AIDUMEM_NEW_SESSION_MAX="${AIDUMEM_NEW_SESSION_MAX:-8}"

PAYLOAD=$(cat)

# ── 解析 payload ─────────────────────────────────────────────────
# Hermes pre_llm_call 的真实形状（v0.20）：
#   {"hook_event_name":"pre_llm_call","session_id":...,"cwd":...,
#    "extra":{"user_message":"...","conversation_history":[...]}}
# 兼容旧形状：顶层 user_message / messages / conversation_history
PARSED=$(printf '%s' "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('0'); print(''); sys.exit(0)
ex = d.get('extra') or {}
msg = ex.get('user_message') or d.get('user_message') or ''
msgs = (ex.get('conversation_history')
        or d.get('conversation_history')
        or ex.get('messages')
        or d.get('messages')
        or [])
print(len(msgs) if isinstance(msgs, list) else 0)
print(msg.replace('\n', ' ') if isinstance(msg, str) else '')
" 2>/dev/null) || PARSED=$'0\n'

MSG_COUNT=$(printf '%s' "$PARSED" | sed -n '1p')
USER_MESSAGE=$(printf '%s' "$PARSED" | sed -n '2,$p')
[ -z "$MSG_COUNT" ] && MSG_COUNT=0

# 太短不注入
if [ "${#USER_MESSAGE}" -lt 3 ]; then
    echo '{}'
    exit 0
fi

# 短会话不注入，省 token
if [ "$MSG_COUNT" -lt "$AIDUMEM_MIN_HISTORY" ]; then
    echo '{}'
    exit 0
fi

# ── 统一取块 helper ──────────────────────────────────────────────
# $1 = 路径, $2 = POST body（空则无 body）
_fetch_ctx() {
    local path="$1" body="${2:-}"
    AIDUMEM_PATH="$path" AIDUMEM_BODY="$body" python3 -c "
import json, os, sys, urllib.request

url = os.environ['AIDUMEM_URL'].rstrip('/') + os.environ['AIDUMEM_PATH']
body = os.environ.get('AIDUMEM_BODY') or ''
data = body.encode('utf-8') if body else None
headers = {'Content-Type': 'application/json'} if data else {}
try:
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=float(os.environ['AIDUMEM_TIMEOUT'])) as resp:
        result = json.loads(resp.read().decode('utf-8'))
except Exception:
    sys.exit(0)

# inject 端点直接给 context；/search 给 results 需自行拼块
ctx = result.get('context') or ''
if ctx:
    print(ctx)
    sys.exit(0)

results = result.get('results') or []
if results:
    limit = int(os.environ['AIDUMEM_SEARCH_LIMIT'])
    lines = ['[aiduMEM Recall]']
    for r in results[:limit]:
        mem = r.get('memory') or r.get('text') or ''
        if mem:
            lines.append('· ' + mem[:120])
    if len(lines) > 1:
        print('\n'.join(lines))
" 2>/dev/null
}

BLOCKS=()

# 1. CoreMemory（每轮）
CORE_CTX=$(_fetch_ctx "/api/core-memory/inject" "")
[ -n "$CORE_CTX" ] && BLOCKS+=("$CORE_CTX")

# 2. Checkpoint（仅新会话首轮）
if [ "$MSG_COUNT" -le "$AIDUMEM_NEW_SESSION_MAX" ]; then
    CP_CTX=$(_fetch_ctx "/api/checkpoint/inject" "")
    [ -n "$CP_CTX" ] && BLOCKS+=("$CP_CTX")
fi

# 3. 相关性检索
SEARCH_BODY=$(AIDUMEM_MSG="$USER_MESSAGE" python3 -c "
import json, os
print(json.dumps({
    'query': os.environ['AIDUMEM_MSG'],
    'user_id': os.environ['AIDUMEM_USER_ID'],
    'limit': int(os.environ['AIDUMEM_SEARCH_LIMIT']),
    'metadata': {},
}, ensure_ascii=False))
" 2>/dev/null)
if [ -n "$SEARCH_BODY" ]; then
    SEARCH_CTX=$(_fetch_ctx "/search" "$SEARCH_BODY")
    [ -n "$SEARCH_CTX" ] && BLOCKS+=("$SEARCH_CTX")
fi

# ── 输出 ─────────────────────────────────────────────────────────
if [ "${#BLOCKS[@]}" -eq 0 ]; then
    echo '{}'
    exit 0
fi

printf '%s\n' "${BLOCKS[@]}" | python3 -c "
import json, sys
ctx = sys.stdin.read().strip()
print(json.dumps({'context': ctx}, ensure_ascii=False) if ctx else '{}')
"

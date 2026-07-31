#!/usr/bin/env bash
# mem0-inject.sh — Phase 4C Shell Hook for pre_llm_call event
# =====================================================================
#
# 从 stdin 读 Hermes 的 pre_llm_call payload (JSON)，
# 拿 user_message 调 aiduMEM /facts/inject-context API，
# 把 top-K 相关 facts 拼成 context 块，stdout 输出 {"context": "..."}，
# Hermes 会自动把它拼到下一轮 LLM 的 user message 后面。
#
# 配置位置: ~/.hermes/agent-hooks/mem0-inject.sh  (本脚本)
# 注册位置: ~/.hermes/config.yaml hooks.pre_llm_call
# 重启 gateway 才生效。
#
# 设计原则:
#   - 绝不影响 LLM 调用 (2s 硬超时 + 任何异常都输出 {})
#   - 短消息不注入 (< 3 字符，避免噪音)
#   - 0 结果不注入 (避免污染 context)
#   - 失败 = 静默 + 输出 {} (subprocess 不会 abort 整个 LLM loop)
#
# 启用前必读: integrations/INTEGRATION_GUIDE.md

set -e

# 读取 stdin payload
PAYLOAD=$(cat)

# 解析 user_message (用 python 因为 bash 处理 JSON 不便)
USER_MESSAGE=$(echo "$PAYLOAD" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    msg = d.get('extra', {}).get('user_message', '') or d.get('user_message', '')
    print(msg)
except Exception:
    print('')
")

# 太短不注入
LEN=${#USER_MESSAGE}
if [ "$LEN" -lt 3 ]; then
    echo '{}'
    exit 0
fi

# 调 mem0 inject-context API (Python 处理 JSON 干净)
RESULT=$(echo "$USER_MESSAGE" | python3 -c "
import json, sys
import urllib.request
import urllib.error

msg = sys.stdin.read().strip()
if not msg:
    print('{}')
    sys.exit(0)

payload = json.dumps({
    'query': msg,
    'k': 5,
    'min_trust': 0.5,
    'max_tokens': 600
}).encode('utf-8')

req = urllib.request.Request(
    'http://127.0.0.1:8767/facts/inject-context',
    data=payload,
    headers={'Content-Type': 'application/json'},
    method='POST',
)

try:
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        result = json.loads(resp.read().decode('utf-8'))
        block = result.get('context_block', '')
        facts = result.get('facts_used', [])
        # 只有真正有 facts 才注入
        if block and facts:
            print(json.dumps({'context': block}, ensure_ascii=False))
        else:
            print('{}')
except Exception:
    # 任何失败 = 静默 + 不注入
    print('{}')
" 2>/dev/null)

# 输出 stdout (Hermes 读这个)
echo "$RESULT"

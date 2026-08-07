#!/usr/bin/env bash
# aidumem-on-save.sh — File Save Hook for Cursor / VS Code / Any Editor
# ======================================================================
# 当代码文件保存时，把文件内容存入 aiduMEM Raw Drawer（原味抽屉）。
# 适合 Python / TypeScript / Go / Rust 等代码文件的自动记忆。
#
# 用法：
#   aidumem-on-save [FILE_PATH] [--summary "optional description"]
#
# Cursor 集成（Terminal 手动触发）：
#   aidumem-on-save ./my_module.py
#
# VS Code Task（tasks.json）：
#   "command": "aidumem-on-save ${file}"
#
# 环境变量：
#   AIDUMEM_URL      API 地址，默认 http://127.0.0.1:8767
#   AIDUMEM_USER_ID  用户命名空间，默认 default
#   AIDUMEM_MAX_SIZE 最大字节数（超出截断），默认 8000
#
# 退出码：0 = 成功或跳过，1 = 文件不存在

set -euo pipefail

FILE_PATH="${1:-}"
SUMMARY="${2:-}"

AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}"
AIDUMEM_USER_ID="${AIDUMEM_USER_ID:-default}"
AIDUMEM_MAX_SIZE="${AIDUMEM_MAX_SIZE:-8000}"
TIMEOUT="${AIDUMEM_TIMEOUT:-5}"

# ── 参数校验 ──────────────────────────────────────────
if [[ -z "$FILE_PATH" ]]; then
    echo "用法: $(basename "$0") <file_path> [description]" >&2
    exit 1
fi

if [[ ! -f "$FILE_PATH" ]]; then
    echo "文件不存在: $FILE_PATH" >&2
    exit 1
fi

# ── 文件过滤：只处理代码文件 ──────────────────────────
EXT="${FILE_PATH##*.}"
SKIP_EXTS="md txt log yaml yml json lock png jpg svg gif ico woff ttf"
for skip in $SKIP_EXTS; do
    if [[ "$EXT" == "$skip" ]]; then
        exit 0  # 静默跳过非代码文件
    fi
done

# ── 读取文件内容（截断超大文件）──────────────────────
CONTENT=$(head -c "$AIDUMEM_MAX_SIZE" "$FILE_PATH" 2>/dev/null || true)
if [[ -z "$CONTENT" ]]; then
    exit 0
fi

# ── 构建存储内容 ──────────────────────────────────────
REL_PATH="${FILE_PATH#$PWD/}"  # 相对路径（若在项目目录下）
STORE_TEXT="FILE: ${REL_PATH}
LINES: $(wc -l < "$FILE_PATH")
${SUMMARY:+DESCRIPTION: $SUMMARY
}
---
${CONTENT}"

# ── 发送到 aiduMEM Raw Drawer ──────────────────────────
PAYLOAD=$(python3 -c "
import json, sys
content = sys.stdin.read()
print(json.dumps({
    'content': content,
    'source': 'cursor_hook',
    'user_id': '${AIDUMEM_USER_ID}'
}))
" <<< "$STORE_TEXT")

HTTP_CODE=$(curl -s -o /tmp/aidumem_resp.json -w "%{http_code}" \
    --max-time "$TIMEOUT" \
    -X POST "${AIDUMEM_URL}/add/raw" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>/dev/null || echo "000")

if [[ "$HTTP_CODE" == "200" ]]; then
    ID=$(python3 -c "import json; d=json.load(open('/tmp/aidumem_resp.json')); print(d.get('id','?'))" 2>/dev/null || echo "?")
    echo "✅ aiduMEM: ${REL_PATH} → Raw Drawer [${ID}]"
else
    echo "⚠️  aiduMEM: 存储失败 (HTTP ${HTTP_CODE})" >&2
fi

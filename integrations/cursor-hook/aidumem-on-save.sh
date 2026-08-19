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
#   AIDUMEM_URL        API 地址，默认 http://127.0.0.1:8767
#   AIDUMEM_API_TOKEN  API 鉴权 token（服务端开启门禁后必需）
#   AIDUMEM_ENV_FILE   .env 路径，用于兜底读取 token 与身份
#   AIDUMEM_USER_ID    用户命名空间，可由 .env 兜底，默认 default
#   AIDUMEM_DEFAULT_USER_ID  AIDUMEM_USER_ID 缺省时的回落值（服务端同名键）
#   AIDUMEM_MAX_SIZE   最大字节数（超出截断），默认 8000
#
# 退出码：0 = 成功或跳过，1 = 文件不存在

set -euo pipefail

FILE_PATH="${1:-}"
SUMMARY="${2:-}"

AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}"
AIDUMEM_MAX_SIZE="${AIDUMEM_MAX_SIZE:-8000}"
TIMEOUT="${AIDUMEM_TIMEOUT:-5}"

# ── 凭据与身份：环境变量 → .env 兜底（v19.4.2 补齐）────────────
# 编辑器/任务运行器拉起本脚本时环境往往是空的，只认环境变量等于不认。
# 门禁开启后不带凭据 = 每次保存都 401，而失败只打印一行警告，很容易被忽略。
#
# 身份必须走同一条链。只让 token 兜底、身份仍只认环境变量的话，空环境下
# 就是「凭据对、租户错」：写入会成功，记忆落进 default 分区，而用户在自己的
# 分区里查 —— 看着「存了」，就是搜不到。这比 401 更难发现，因为它连警告都没有。
#
# ⚠️ 本文件设计为可拷贝到编辑器配置里独立运行，因此不 import 仓库代码，
#    自带一份最小 .env 解析（与 integrations/aidumem-inject.sh 同一套逻辑）。
_read_env_key() {
    local f="$1" key="$2"
    [ -n "$f" ] && [ -f "$f" ] && [ -r "$f" ] || return 1
    local v
    v=$(sed -n "s/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}${key}[[:space:]]*=[[:space:]]*//p" \
            "$f" 2>/dev/null | head -1 | tr -d '\r' \
        | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")
    [ -n "$v" ] || return 1
    printf '%s' "$v"
}

# 从 .env 链里取某个键的第一个定义；全链未定义则返回非 0。
_lookup_env_key() {
    local key="$1" cand val
    for cand in "${AIDUMEM_ENV_FILE:-}" \
                "${AIDUMEM_HOME:+${AIDUMEM_HOME}/.env}" \
                "${HOME:+${HOME}/.aidumem/.env}" \
                "./.env"; do
        if val=$(_read_env_key "$cand" "$key"); then
            printf '%s' "$val"
            return 0
        fi
    done
    return 1
}

if [ -z "${AIDUMEM_API_TOKEN:-}" ]; then
    AIDUMEM_API_TOKEN=$(_lookup_env_key AIDUMEM_API_TOKEN) || AIDUMEM_API_TOKEN=""
fi

AIDUMEM_USER_ID="${AIDUMEM_USER_ID:-${AIDUMEM_DEFAULT_USER_ID:-}}"
if [ -z "$AIDUMEM_USER_ID" ]; then
    AIDUMEM_USER_ID=$(_lookup_env_key AIDUMEM_USER_ID) \
        || AIDUMEM_USER_ID=$(_lookup_env_key AIDUMEM_DEFAULT_USER_ID) \
        || AIDUMEM_USER_ID="default"
fi

AUTH_ARGS=()
if [ -n "${AIDUMEM_API_TOKEN:-}" ]; then
    AUTH_ARGS=(-H "Authorization: Bearer ${AIDUMEM_API_TOKEN}")
fi

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

# ${AUTH_ARGS[@]+...} 写法是为了在 bash 3.2（macOS 自带）下，
# 空数组遇上 `set -u` 不会报 "unbound variable"。
HTTP_CODE=$(curl -s -o /tmp/aidumem_resp.json -w "%{http_code}" \
    --max-time "$TIMEOUT" \
    -X POST "${AIDUMEM_URL}/add/raw" \
    -H "Content-Type: application/json" \
    ${AUTH_ARGS[@]+"${AUTH_ARGS[@]}"} \
    -d "$PAYLOAD" 2>/dev/null || echo "000")

if [[ "$HTTP_CODE" == "200" ]]; then
    ID=$(python3 -c "import json; d=json.load(open('/tmp/aidumem_resp.json')); print(d.get('id','?'))" 2>/dev/null || echo "?")
    echo "✅ aiduMEM: ${REL_PATH} → Raw Drawer [${ID}]"
elif [[ "$HTTP_CODE" == "401" || "$HTTP_CODE" == "403" ]]; then
    # 单独点名鉴权失败：这是最常见也最容易误判为「服务挂了」的一种失败。
    echo "⚠️  aiduMEM: 鉴权失败 (HTTP ${HTTP_CODE})。请设置 AIDUMEM_API_TOKEN，" >&2
    echo "    或让 AIDUMEM_ENV_FILE 指向部署的 .env（当前未读到 token）。" >&2
else
    echo "⚠️  aiduMEM: 存储失败 (HTTP ${HTTP_CODE})" >&2
fi

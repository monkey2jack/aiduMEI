#!/usr/bin/env bash
# aidumem-cc-inject.sh — Claude Code `UserPromptSubmit` shell hook（读线）
# =====================================================================
# 把 aiduMEI 的记忆注入 Claude Code 当前轮上下文：
#   1. CoreMemory —— 常驻结构化 block（身份 / 当前项目 / 关键决策）
#   2. /search   —— 按当轮 prompt 做相关性检索
# hook 的 stdout 会被 Claude Code 自动注入本轮上下文；无内容时静默。
#
# stdin  : Claude Code UserPromptSubmit payload (JSON: prompt/session_id/...)
# stdout : 注入文本（或空）
#
# 与另两条线的关系（三线同租户同凭据，身份解析逐行同源）：
#   读线   aidumem-cc-inject.sh  (UserPromptSubmit) —— 把旧记忆喂给模型
#   写线   aidumem-cc-ingest.sh  (Stop)             —— 把这一轮存回去
#   萃取线 aidumem-cc-distill.sh (SessionEnd)       —— 把这一程提炼成一条
#
# 配置（全部可选，均有默认；身份解析优先级与 Hermes 版真源一致）：
#   AIDUMEM_URL             服务地址，默认 http://127.0.0.1:8767
#   AIDUMEM_USER_ID         记忆归属用户，可由 .env 兜底，默认 default
#   AIDUMEM_DEFAULT_USER_ID AIDUMEM_USER_ID 缺省时的回落值（服务端同名键）
#   AIDUMEI_BANK_ID         记忆库，可由 .env 兜底，默认 default
#   AIDUMEM_API_TOKEN       服务门禁 token，可由 .env 兜底
#   AIDUMEM_ENV_FILE        .env 路径候选之一
#   AIDUMEI_CC_SESSION_PREFIX  会话 id 前缀，默认 cc-（写线/萃取线必须同值）
#
# 设计原则（与 Hermes 版三线同源）：
#   - 绝不拖累宿主：任何异常静默 exit 0，不阻塞对话
#   - 熔断防雪崩：探活不通 → 打标记，5 秒内直接放行
#     （f0.4++：探活超时 1s。0.3s 曾在服务高峰期写向量时误超时触发熔断）
#   - 注入安全：prompt/query 一律经环境变量传给 python，
#     绝不做 shell 字符串拼接（内容含引号即破语法 = 静默失败）
set -uo pipefail

AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}"

# ── 凭据与身份（与写线/萃取线逐行同源）─────────────────────
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

_hook_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd) || _hook_dir=""
_env_candidates() {
    printf '%s\n' "${AIDUMEM_ENV_FILE:-}" \
                  "${AIDUMEM_HOME:+${AIDUMEM_HOME}/.env}" \
                  "${HOME:+${HOME}/.aidumem/.env}" \
                  "${_hook_dir:+${_hook_dir}/../../.env}" \
                  "./.env"
}

_lookup_env_key() {
    local key="$1" cand val
    while IFS= read -r cand; do
        if val=$(_read_env_key "$cand" "$key"); then
            printf '%s' "$val"; return 0
        fi
    done <<EOF
$(_env_candidates)
EOF
    return 1
}

if [ -z "${AIDUMEM_API_TOKEN:-}" ]; then
    AIDUMEM_API_TOKEN=$(_lookup_env_key AIDUMEM_API_TOKEN) || AIDUMEM_API_TOKEN=""
fi
_uid="${AIDUMEM_USER_ID:-${AIDUMEM_DEFAULT_USER_ID:-}}"
if [ -z "$_uid" ]; then
    _uid=$(_lookup_env_key AIDUMEM_USER_ID) \
        || _uid=$(_lookup_env_key AIDUMEM_DEFAULT_USER_ID) \
        || _uid="default"
fi
_bid="${AIDUMEI_BANK_ID:-}"
if [ -z "$_bid" ]; then
    _bid=$(_lookup_env_key AIDUMEI_BANK_ID) || _bid="default"
fi

# ── 自检（--selftest）：验证空环境下凭据+身份都从 .env 兜底读到了 ──────────
# 必须在读 stdin 之前：下面 RAW_INPUT=$(cat) 会阻塞等 stdin。
# 发一个带 user_id 的 /search，让部署方确认「网关/cron 以空环境拉起本 hook 时，
# 身份不是悄悄退回 default、凭据不是空串」—— v19.4.1 全线 401 的教训。
if [ "${1:-}" = "--selftest" ]; then
  AIDUMEI_HOOK_UID="$_uid" AIDUMEI_HOOK_BID="$_bid" \
  AIDUMEI_HOOK_TOKEN="${AIDUMEM_API_TOKEN:-}" AIDUMEI_HOOK_URL="$AIDUMEM_URL" \
  python3 -c '
import json, os, sys, urllib.request
uid = os.environ["AIDUMEI_HOOK_UID"]; bid = os.environ["AIDUMEI_HOOK_BID"]
tok = os.environ.get("AIDUMEI_HOOK_TOKEN", ""); url = os.environ["AIDUMEI_HOOK_URL"].rstrip("/")
body = json.dumps({"query": "aidumem inject selftest", "user_id": uid,
                   "caller_user_id": uid, "bank_id": bid, "limit": 1}).encode()
h = {"Content-Type": "application/json"}
if tok: h["Authorization"] = "Bearer " + tok
req = urllib.request.Request(url + "/search", data=body, headers=h)
try:
    urllib.request.urlopen(req, timeout=10)
    print("[aidumem-cc-inject] selftest OK user_id=%s token=%s"
          % (uid, "present" if tok else "absent"))
except Exception as e:
    sys.stderr.write("[aidumem-cc-inject] selftest FAILED %s\n" % type(e).__name__)
    sys.exit(3)
'
  exit $?
fi

# ── 熔断：探活不通直接放行，5s 冷却 ─────────────────────────
CIRCUIT="${TMPDIR:-/tmp}/.aidumem_cc_circuit"
NOW=$(date +%s)
if [ -f "$CIRCUIT" ] && [ $((NOW - $(stat -c %Y "$CIRCUIT" 2>/dev/null || stat -f %m "$CIRCUIT" 2>/dev/null || echo 0))) -lt 5 ]; then
  exit 0
fi

# ── 读 stdin 拿当轮 prompt（环境变量传递，零字符串拼接）──────
RAW_INPUT=$(cat)
PROMPT=$(AIDUMEI_HOOK_INPUT="$RAW_INPUT" python3 -c '
import json, os
try:
    d = json.loads(os.environ.get("AIDUMEI_HOOK_INPUT", ""))
    print(str(d.get("prompt", ""))[:500])
except Exception:
    pass' 2>/dev/null)
[ -z "$PROMPT" ] && exit 0

# ── 凭据头（token 经 stdin 交给 curl，不上命令行 —— ps 对本机所有用户可见）──
# 门禁可能开启，探活与读取都带凭据；未配 token 时 header 为空，行为同门禁未开。
AUTH_ARGS=(-H @-)
_AUTH_HEADER=""
[ -n "${AIDUMEM_API_TOKEN:-}" ] && _AUTH_HEADER="Authorization: Bearer ${AIDUMEM_API_TOKEN}"

# 服务探活（1s；仍远小于宿主 hook 超时上限）
printf '%s\n' "$_AUTH_HEADER" | curl -s -m 1 "${AUTH_ARGS[@]}" "$AIDUMEM_URL/health" >/dev/null 2>&1 || { touch "$CIRCUIT"; exit 0; }

# ── 1) CoreMemory 注入 ─────────────────────────────────────
CORE=$(printf '%s\n' "$_AUTH_HEADER" | curl -s -m 2 -G "${AUTH_ARGS[@]}" \
  --data-urlencode "user_id=$_uid" --data-urlencode "bank_id=$_bid" \
  "$AIDUMEM_URL/api/core-memory" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    blocks = d.get("blocks") or {}
    parts = []
    for k, v in blocks.items():
        if isinstance(v, dict) and v.get("content"):
            parts.append("【CoreMemory·%s】%s" % (k, str(v["content"])[:200]))
    print("\n".join(parts[:3]))
except Exception:
    pass' 2>/dev/null)

# ── 2) 相关性检索（query 经环境变量传递，防注入）─────────────
RECALL=$(AIDUMEI_HOOK_QUERY="$PROMPT" AIDUMEI_HOOK_TOKEN="$AIDUMEM_API_TOKEN" \
  AIDUMEI_HOOK_URL="$AIDUMEM_URL" AIDUMEI_HOOK_UID="$_uid" AIDUMEI_HOOK_BID="$_bid" \
  python3 -c '
import json, os, sys, urllib.request
q = os.environ.get("AIDUMEI_HOOK_QUERY", "")
url = os.environ.get("AIDUMEI_HOOK_URL", "")
tok = os.environ.get("AIDUMEI_HOOK_TOKEN", "")
uid = os.environ.get("AIDUMEI_HOOK_UID", "default")
bid = os.environ.get("AIDUMEI_HOOK_BID", "default")
if not q or not url:
    sys.exit()
body = json.dumps({"query": q, "user_id": uid, "caller_user_id": uid,
                   "bank_id": bid, "limit": 3}).encode()
headers = {"Content-Type": "application/json"}
if tok:
    headers["Authorization"] = "Bearer " + tok
req = urllib.request.Request(url.rstrip("/") + "/search", data=body, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=4) as r:
        d = json.loads(r.read())
    for item in (d.get("results") or [])[:3]:
        m = item.get("memory") if isinstance(item, dict) else str(item)
        print("· " + str(m or item)[:150])
    # f0.5（fork 审计 F06）：降级照实透出——灯坏了不能冒充「没有记忆」。
    # 服务端契约：recall_verdict=degraded 时 results 不完整，degraded 列表
    # 载明哪个组件出了问题。静默失败是本 hook 的纪律，但「静默」只许用于
    # 连不上（网络层）；服务端明说了召回异常，就必须让宿主知道。
    if d.get("recall_verdict") == "degraded":
        parts = d.get("degraded") or []
        detail = ", ".join(str(p) for p in parts[:3]) if isinstance(parts, list) else str(parts)
        print("⚠ [aiduMEI·检索降级: " + (detail or "部分组件异常") + "；以下结果不完整，不代表没有相关记忆]")
except Exception:
    pass' 2>/dev/null)

# ── 组装注入（静默失败，绝不阻塞对话）───────────────────────
OUT=""
[ -n "$CORE" ] && OUT="🧠[aiduMEI·CoreMemory]
$CORE"
[ -n "$RECALL" ] && OUT="$OUT

📚[aiduMEI·相关记忆]
$RECALL"
[ -n "$OUT" ] && printf '%s\n' "$OUT"
exit 0

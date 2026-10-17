#!/usr/bin/env bash
# aidumem-codex-inject.sh — Codex CLI `UserPromptSubmit` hook（读线）
# =====================================================================
# 每轮之前把 aiduMEI 的记忆喂给模型：
#   1. CoreMemory —— 常驻结构化 block（身份 / 当前项目 / 关键决策）
#   2. /search   —— 按当轮 prompt 做相关性检索
# stdout 的纯文本会被 Codex 作为额外 developer context 注入本轮。
#
# stdin  : Codex UserPromptSubmit payload
#          {session_id, turn_id, transcript_path, cwd, hook_event_name,
#           model, permission_mode, prompt}
# stdout : 注入文本（或空）；JSON 形式亦被支持，这里用纯文本保持最简
#
# 与另两条线的关系（三线同租户同凭据，身份解析逐行同源）：
#   读线   aidumem-codex-inject.sh  (UserPromptSubmit) —— 把旧记忆喂给模型
#   写线   aidumem-codex-ingest.sh (Stop)             —— 把这一轮存回去
#   萃取线 aidumem-codex-distill.sh (SessionEnd)       —— 把这一程提炼成一条
#
# 配置（全部可选，均有默认；身份解析优先级与 Hermes/cc 版真源一致）：
#   AIDUMEM_URL             服务地址，默认 http://127.0.0.1:8767
#   AIDUMEM_USER_ID         记忆归属用户，可由 .env 兜底，默认 default
#   AIDUMEM_DEFAULT_USER_ID AIDUMEM_USER_ID 缺省时的回落值（服务端同名键）
#   AIDUMEI_BANK_ID         记忆库，可由 .env 兜底，默认 default
#   AIDUMEM_API_TOKEN       服务门禁 token，可由 .env 兜底
#   AIDUMEM_ENV_FILE        .env 路径候选之一
#   AIDUMEI_CODEX_SESSION_PREFIX  会话 id 前缀，默认 codex-（三线必须同值）
#
# 设计原则（与 Hermes/cc 版三线同源）：
#   - 绝不拖累宿主：任何异常静默 exit 0，不阻塞对话
#   - 熔断防雪崩：探活不通 → 打标记，5 秒内直接放行
#   - 注入安全：prompt/query 一律经环境变量传给 python，
#     绝不做 shell 字符串拼接（内容含引号即破语法 = 静默失败）
set -uo pipefail

AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}"
SESSION_PREFIX="${AIDUMEI_CODEX_SESSION_PREFIX-codex-}"

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
_diag() { [ -n "${AIDUMEM_HOOK_QUIET:-}" ] || printf '%s\n' "$*" >&2; }

# ── 自检（--selftest）：验证空环境下凭据+身份都从 .env 兜底读到了 ──────────
if [ "${1:-}" = "--selftest" ]; then
  AIDUMEI_HOOK_UID="$_uid" AIDUMEI_HOOK_BID="$_bid" \
  AIDUMEI_HOOK_TOKEN="${AIDUMEM_API_TOKEN:-}" AIDUMEI_HOOK_URL="$AIDUMEM_URL" \
  python3 -c '
import json, os, sys, urllib.request
uid = os.environ["AIDUMEI_HOOK_UID"]; bid = os.environ["AIDUMEI_HOOK_BID"]
tok = os.environ.get("AIDUMEI_HOOK_TOKEN", ""); url = os.environ["AIDUMEI_HOOK_URL"].rstrip("/")
body = json.dumps({"query": "aidumem codex inject selftest", "user_id": uid,
                   "caller_user_id": uid, "bank_id": bid, "limit": 1}).encode()
h = {"Content-Type": "application/json"}
if tok: h["Authorization"] = "Bearer " + tok
req = urllib.request.Request(url + "/search", data=body, headers=h)
try:
    urllib.request.urlopen(req, timeout=10)
    print("[aidumem-codex-inject] selftest OK user_id=%s token=%s"
          % (uid, "present" if tok else "absent"))
except Exception as e:
    sys.stderr.write("[aidumem-codex-inject] selftest FAILED %s\n" % type(e).__name__)
    sys.exit(3)
'
  exit $?
fi

# ── 熔断：探活不通直接放行，5s 冷却 ─────────────────────────
CIRCUIT="${TMPDIR:-/tmp}/.aidumem_codex_circuit"
NOW=$(date +%s)
if [ -f "$CIRCUIT" ] && [ $((NOW - $(stat -c %Y "$CIRCUIT" 2>/dev/null || stat -f %m "$CIRCUIT" 2>/dev/null || echo 0))) -lt 5 ]; then
  exit 0
fi

# ── 读 stdin 拿当轮 prompt + turn_id（环境变量传递，零字符串拼接）──────
RAW_INPUT=$(cat)
PARSED=$(AIDUMEI_HOOK_INPUT="$RAW_INPUT" AIDUMEI_HOOK_PREFIX="$SESSION_PREFIX" python3 -c '
import json, os
try:
    d = json.loads(os.environ.get("AIDUMEI_HOOK_INPUT", ""))
except Exception:
    raise SystemExit
prompt = str(d.get("prompt", ""))[:500]
sid = str(d.get("session_id") or "")[:256]
tid = str(d.get("turn_id") or "")[:256]
prefix = os.environ.get("AIDUMEI_HOOK_PREFIX", "codex-")
print(json.dumps({"prompt": prompt, "session_id": (prefix + sid) if sid else "",
                  "turn_id": tid}, ensure_ascii=False))
' 2>/dev/null)
[ -z "$PARSED" ] && exit 0
PROMPT=$(AIDUMEI_HOOK_PARSED="$PARSED" python3 -c 'import json,os;print(json.loads(os.environ["AIDUMEI_HOOK_PARSED"]).get("prompt",""))' 2>/dev/null)
[ -z "$PROMPT" ] && exit 0

# ── 把本轮 prompt 暂存，供写线（Stop）配对使用 ─────────────────
# Stop payload 只带 last_assistant_message，不带用户原话；transcript 格式
# 官方声明「不是稳定接口」。用 turn_id 做键的临时文件把两线接起来，
# 写线读完即删；残留文件由写线的过期清扫兜底（静默失败≠失声，见写线）。
_spool_dir="${AIDUMEI_CODEX_SPOOL_DIR:-${TMPDIR:-/tmp}/aidumem-codex-spool}"
mkdir -p "$_spool_dir" 2>/dev/null || _spool_dir=""
if [ -n "$_spool_dir" ]; then
  AIDUMEI_HOOK_PARSED="$PARSED" AIDUMEI_HOOK_SPOOL="$_spool_dir" python3 -c '
import json, os, time
p = json.loads(os.environ["AIDUMEI_HOOK_PARSED"])
tid = p.get("turn_id") or p.get("session_id")
if not tid:
    raise SystemExit
path = os.path.join(os.environ["AIDUMEI_HOOK_SPOOL"], "turn-" + tid + ".json")
try:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"prompt": p.get("prompt", ""), "session_id": p.get("session_id", ""),
                   "ts": time.time()}, f, ensure_ascii=False)
except OSError:
    pass
' 2>/dev/null
fi

# ── 凭据头（token 经 stdin 交给 curl，不上命令行 —— ps 对本机所有用户可见）──
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

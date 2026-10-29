#!/usr/bin/env bash
# aidumem-codex-ingest.sh — Codex CLI `Stop` hook（写线）
# =====================================================================
# 每轮对话结束时，摘取本轮 Q&A，异步 POST /add 沉淀进 aiduMEI。
#
# stdin  : Codex Stop payload
#          {session_id, turn_id, transcript_path, cwd, hook_event_name,
#           model, permission_mode, stop_hook_active, last_assistant_message}
# stdout : {}   —— Stop 事件要求 exit 0 时输出 JSON；纯文本无效
#
# 用户原话不在 Stop payload 里（Codex 只给 last_assistant_message），
# transcript 格式官方声明「不是稳定接口」。所以读线（UserPromptSubmit）
# 把本轮 prompt 按 turn_id 暂存到 spool，写线在此配对读取、用完即删。
# spool 缺失时退化为「只存 assistant 侧」，绝不因此丢整轮（安静≠失声，
# 跳过与失败都往 stderr 写一行结构化诊断）。
#
# 沉淀必须携带 session_id —— 服务端把 /add 原文落 verbatim_turns 时按
# metadata 的 session_id 归程，而萃取线（distill_session）正是用它圈定
# 「这一程」的原文。写线不传 session_id，萃取线就永远圈不到来源：
# 每轮记忆照常进、探针全绿、精华却从未产生（静默失败铁律的活案例）。
# session_id 口径 = ${AIDUMEI_CODEX_SESSION_PREFIX}<sid>，与萃取线
# aidumem-codex-distill.sh 逐字一致（两线各有回归钉死）。
#
# 配置（与读线/萃取线共用同一套键，三线必须同租户同凭据）：
#   AIDUMEM_URL / AIDUMEM_USER_ID / AIDUMEM_DEFAULT_USER_ID / AIDUMEI_BANK_ID
#   AIDUMEM_API_TOKEN / AIDUMEM_ENV_FILE / AIDUMEM_LOG_DIR
#   AIDUMEI_CODEX_SESSION_PREFIX  会话 id 前缀，默认 codex-
#   AIDUMEI_CODEX_SPOOL_DIR       读写线配对目录，默认 ${TMPDIR:-/tmp}/aidumem-codex-spool
#
# 设计原则：
#   - 绝不拖累宿主：沉淀异步后台发出，hook 立即返回；任何异常静默 exit 0
#   - 安静≠失声：跳过与失败往 stderr 写一行结构化诊断
set -uo pipefail
# f0.7 P3：新前缀别名（connect_host 写 AIDUMEI_URL；AIDUMEM_URL 设了仍优先）
if [ -z "${AIDUMEM_URL:-}" ] && [ -n "${AIDUMEI_URL:-}" ]; then export AIDUMEM_URL="$AIDUMEI_URL"; fi

AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}"
SESSION_PREFIX="${AIDUMEI_CODEX_SESSION_PREFIX-codex-}"
SPOOL_DIR="${AIDUMEI_CODEX_SPOOL_DIR:-${TMPDIR:-/tmp}/aidumem-codex-spool}"

# ── 凭据与身份（与读线/萃取线逐行同源）─────────────────────
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
# 必须在读 stdin 之前：下面 RAW_INPUT=$(cat) 会阻塞等 stdin。
# 写线的身份读错比读线更毒——读错表现为「搜不到」有人会查，写错表现为
# 「一切正常」记忆安静进了别人的库。故此处用解析出的 _uid 打一次只读 /search，
# 确认兜底身份非空且凭据在场。
if [ "${1:-}" = "--selftest" ]; then
  AIDUMEI_HOOK_UID="$_uid" AIDUMEI_HOOK_BID="$_bid" \
  AIDUMEI_HOOK_TOKEN="${AIDUMEM_API_TOKEN:-}" AIDUMEI_HOOK_URL="$AIDUMEM_URL" \
  python3 -c '
import json, os, sys, urllib.request
uid = os.environ["AIDUMEI_HOOK_UID"]; bid = os.environ["AIDUMEI_HOOK_BID"]
tok = os.environ.get("AIDUMEI_HOOK_TOKEN", ""); url = os.environ["AIDUMEI_HOOK_URL"].rstrip("/")
body = json.dumps({"query": "aidumem codex ingest selftest", "user_id": uid,
                   "caller_user_id": uid, "bank_id": bid, "limit": 1}).encode()
h = {"Content-Type": "application/json"}
if tok: h["Authorization"] = "Bearer " + tok
req = urllib.request.Request(url + "/search", data=body, headers=h)
try:
    urllib.request.urlopen(req, timeout=10)
    print("[aidumem-codex-ingest] selftest OK user_id=%s token=%s"
          % (uid, "present" if tok else "absent"))
except Exception as e:
    sys.stderr.write("[aidumem-codex-ingest] selftest FAILED %s\n" % type(e).__name__)
    sys.exit(3)
'
  exit $?
fi

RAW_INPUT=$(cat)

# ── 摘本轮 Q&A + session_id（环境变量传递，零拼接）──────────
PARSED=$(AIDUMEI_HOOK_INPUT="$RAW_INPUT" AIDUMEI_HOOK_PREFIX="$SESSION_PREFIX" \
  AIDUMEI_HOOK_SPOOL="$SPOOL_DIR" \
  AIDUMEI_HOOK_UID="$_uid" AIDUMEI_HOOK_BID="$_bid" python3 -c '
import json, os, sys, time

try:
    d = json.loads(os.environ.get("AIDUMEI_HOOK_INPUT", ""))
except Exception:
    sys.exit()

sid = str(d.get("session_id") or "")[:256]
tid = str(d.get("turn_id") or "")[:256]
assistant = str(d.get("last_assistant_message") or "").strip()
prefix = os.environ.get("AIDUMEI_HOOK_PREFIX", "codex-")
spool = os.environ.get("AIDUMEI_HOOK_SPOOL", "")

# 用户原话：优先读线 spool（turn_id 配对），其次 transcript 兜底
user_text = ""
spool_path = ""
if spool and (tid or sid):
    key = tid or sid
    spool_path = os.path.join(spool, "turn-" + key + ".json")
    try:
        with open(spool_path, encoding="utf-8") as f:
            sp = json.load(f)
        user_text = str(sp.get("prompt") or "").strip()
    except Exception:
        user_text = ""

if not user_text:
    # transcript 兜底：官方声明格式非稳定接口，只做尽力解析，失败即放弃
    tp = d.get("transcript_path")
    if tp and os.path.exists(tp):
        try:
            last_user = ""
            with open(tp, errors="replace") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    p = e.get("payload") or {}
                    if p.get("type") == "message" and p.get("role") == "user":
                        c = p.get("content")
                        if isinstance(c, list):
                            c = " ".join(x.get("text", "") for x in c
                                         if isinstance(x, dict) and x.get("type") in ("input_text", "text"))
                        if isinstance(c, str) and c.strip():
                            t = c.strip()
                            # 跳过 harness 注入块（bridge_context / environment_context 等）
                            if t.startswith("<") and ("bridge_context" in t[:80] or "environment_context" in t[:80]):
                                continue
                            last_user = t
            user_text = last_user
        except Exception:
            user_text = ""

if not user_text and not assistant:
    sys.exit()

messages = []
if user_text:
    messages.append({"role": "user", "content": user_text[:500]})
if assistant:
    messages.append({"role": "assistant", "content": assistant[:500]})
if not messages:
    sys.exit()

# spool 用完即删；顺带清扫超过 6 小时的残留（读线写了、写线没跑到的轮次）
if spool_path:
    try:
        os.unlink(spool_path)
    except OSError:
        pass
if spool and os.path.isdir(spool):
    now = time.time()
    try:
        for name in os.listdir(spool):
            if not name.startswith("turn-") or not name.endswith(".json"):
                continue
            fp = os.path.join(spool, name)
            try:
                if now - os.path.getmtime(fp) > 6 * 3600:
                    os.unlink(fp)
            except OSError:
                continue
    except OSError:
        pass

print(json.dumps({
    # 幂等加前缀：宿主/重放已带前缀时不再叠加（cc-cc- 双前缀，f0.6+++ WP6）
    "session_id": (sid if sid.startswith(prefix) else prefix + sid) if sid else "",
    "user_id": os.environ.get("AIDUMEI_HOOK_UID", "default"),
    "bank_id": os.environ.get("AIDUMEI_HOOK_BID", "default"),
    "messages": messages,
}, ensure_ascii=False))
' 2>/dev/null)

# Stop 事件契约：exit 0 必须输出 JSON。无内容可沉淀时也要给 {}，
# 否则 Codex 记一次 hook failure（安静≠失声，但不能把宿主日志搞脏）。
if [ -z "$PARSED" ]; then
  printf '{}\n'
  exit 0
fi

# ── 异步沉淀（不阻塞宿主；metadata 口径与萃取线圈定字段一致）──
# 诊断不许随 >/dev/null 扔掉（安静≠失声）：失败行落持久日志，粗粒度轮转。
_log_dir="${AIDUMEM_LOG_DIR:-${HOME:+${HOME}/.aidumem/logs}}"
_log_dir="${_log_dir:-/tmp}"
mkdir -p "$_log_dir" 2>/dev/null || _log_dir="/tmp"
_log="${_log_dir}/aidumem-codex-ingest.log"
if [ -f "$_log" ] && [ "$(wc -c <"$_log" 2>/dev/null || echo 0)" -gt 2097152 ]; then
    mv -f "$_log" "${_log}.1" 2>/dev/null || :
fi
AIDUMEI_HOOK_PARSED="$PARSED" AIDUMEI_HOOK_TOKEN="$AIDUMEM_API_TOKEN" \
AIDUMEI_HOOK_URL="$AIDUMEM_URL" python3 -c '
import json, os, urllib.request
p = json.loads(os.environ.get("AIDUMEI_HOOK_PARSED", "{}"))
messages = p.get("messages") or []
if not messages:
    raise SystemExit
uid = p.get("user_id") or "default"
tok = os.environ.get("AIDUMEI_HOOK_TOKEN", "")
url = os.environ.get("AIDUMEI_HOOK_URL", "").rstrip("/")
body = json.dumps({
    "messages": messages,
    "user_id": uid,
    # Scope hardening: bearer-token writes must declare their caller,
    # otherwise cross-hall isolation rejects them with auth_failed 403.
    "caller_user_id": uid,
    "bank_id": p.get("bank_id") or "default",
    "async_mode": True,
    "metadata": {"source": "codex-ingest",
                 "_origin_session_id": p.get("session_id", ""),
                 "_origin_agent": "aidumem-codex-ingest"},
}, ensure_ascii=False).encode()
headers = {"Content-Type": "application/json"}
if tok:
    headers["Authorization"] = "Bearer " + tok
req = urllib.request.Request(url + "/add", data=body, headers=headers)
try:
    urllib.request.urlopen(req, timeout=3)
except Exception as exc:
    import sys
    sys.stderr.write("[aidumem-codex-ingest] add_failed err=%s\n" % type(exc).__name__)
' >>"$_log" 2>&1 &
disown 2>/dev/null || true

printf '{}\n'
exit 0

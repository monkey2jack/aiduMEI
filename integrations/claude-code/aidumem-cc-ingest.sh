#!/usr/bin/env bash
# aidumem-cc-ingest.sh — Claude Code `Stop` shell hook（写线）
# =====================================================================
# 每轮对话结束时，摘取最后一轮 Q&A，异步 POST /add 沉淀进 aiduMEI。
#
# stdin  : Claude Code Stop payload (JSON: session_id/transcript_path/...)
# stdout : （无 —— 写线不注入上下文）
#
# f0.4++ WP4（根因修复）：沉淀必须携带 session_id —— 服务端把 /add 原文落
#   verbatim_turns 时按 metadata 的 session_id 归程，而萃取线
#   （distill_session）正是用它圈定「这一程」的原文。写线不传 session_id，
#   萃取线就永远圈不到来源：每轮记忆照常进、探针全绿、精华却从未产生
#   （静默失败铁律的活案例 —— 2026-10-16 实机审计实锤，1387 条历史原文
#   session_id 全空）。session_id 口径 = ${AIDUMEI_CC_SESSION_PREFIX}<sid>，
#   与萃取线 aidumem-cc-distill.sh 逐字一致（两线各有回归钉死）。
# f0.4++ WP6（注入加固）：payload 经环境变量传给 python，绝不拼进
#   heredoc/字符串 —— transcript 内容含三引号即破语法（原实现 r'''$INPUT'''
#   的实测病灶），破在宿主里还不报错。
#
# 配置（与读线/萃取线共用同一套键，三线必须同租户同凭据）：
#   AIDUMEM_URL / AIDUMEM_USER_ID / AIDUMEM_DEFAULT_USER_ID / AIDUMEI_BANK_ID
#   AIDUMEM_API_TOKEN / AIDUMEM_ENV_FILE
#   AIDUMEI_CC_SESSION_PREFIX  会话 id 前缀，默认 cc-
#
# 设计原则：
#   - 绝不拖累宿主：沉淀异步后台发出，hook 立即返回；任何异常静默 exit 0
#   - 安静≠失声：跳过与失败往 stderr 写一行结构化诊断（宿主不显示，
#     但人工排查时可见）
set -uo pipefail
# f0.7 P3：新前缀别名（connect_host 写 AIDUMEI_URL；AIDUMEM_URL 设了仍优先）
if [ -z "${AIDUMEM_URL:-}" ] && [ -n "${AIDUMEI_URL:-}" ]; then export AIDUMEM_URL="$AIDUMEI_URL"; fi

AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}"
SESSION_PREFIX="${AIDUMEI_CC_SESSION_PREFIX-cc-}"

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
# 确认兜底身份非空且凭据在场（/add 写路径由 tests/test_f04pp_cc_adaptation.py 覆盖）。
if [ "${1:-}" = "--selftest" ]; then
  AIDUMEI_HOOK_UID="$_uid" AIDUMEI_HOOK_BID="$_bid" \
  AIDUMEI_HOOK_TOKEN="${AIDUMEM_API_TOKEN:-}" AIDUMEI_HOOK_URL="$AIDUMEM_URL" \
  python3 -c '
import json, os, sys, urllib.request
uid = os.environ["AIDUMEI_HOOK_UID"]; bid = os.environ["AIDUMEI_HOOK_BID"]
tok = os.environ.get("AIDUMEI_HOOK_TOKEN", ""); url = os.environ["AIDUMEI_HOOK_URL"].rstrip("/")
body = json.dumps({"query": "aidumem cc ingest selftest", "user_id": uid,
                   "caller_user_id": uid, "bank_id": bid, "limit": 1}).encode()
h = {"Content-Type": "application/json"}
if tok: h["Authorization"] = "Bearer " + tok
req = urllib.request.Request(url + "/search", data=body, headers=h)
try:
    urllib.request.urlopen(req, timeout=10)
    print("[aidumem-cc-ingest] selftest OK user_id=%s token=%s"
          % (uid, "present" if tok else "absent"))
except Exception as e:
    sys.stderr.write("[aidumem-cc-ingest] selftest FAILED %s\n" % type(e).__name__)
    sys.exit(3)
'
  exit $?
fi

RAW_INPUT=$(cat)

# ── 摘最后一轮 Q&A + session_id（环境变量传递，零拼接）──────
PARSED=$(AIDUMEI_HOOK_INPUT="$RAW_INPUT" AIDUMEI_HOOK_PREFIX="$SESSION_PREFIX" \
  AIDUMEI_HOOK_UID="$_uid" AIDUMEI_HOOK_BID="$_bid" python3 -c '
import json, os, sys, re
try:
    d = json.loads(os.environ.get("AIDUMEI_HOOK_INPUT", ""))
except Exception:
    sys.exit()
sid = str(d.get("session_id") or "")[:256]
tp = d.get("transcript_path")
if not tp:
    sys.exit()
# bridge/宿主注入的元数据块整体剥离，不进语义层。（2026-10-28 修复：
# aiduCAT 下发的每条用户消息都以 <bridge_context> 开头，旧逻辑整条丢弃
# → 有效用户消息恒为 0 → hook 静默 exit → 写线/萃取线全断。正确做法是
# 剥掉注入壳后提取真正文，正文非空才保留。）
_INJECT_RE = re.compile(
    r"<(bridge_context|bridge_instructions|quoted_message|interactive_card|"
    r"system-reminder|task-notification|pasted_content)[^>]*>.*?</\1\s*>",
    re.S)
_USER_INPUT_RE = re.compile(r"<user_input>(.*?)</user_input>", re.S)
_NOISE = ("stop hook feedback:", "caveat:", "\U0001f4da[aidumei")
# f0.7 P6d：CC 在模型空回复后自动补一句催促（以 user 身份写进 transcript），
# 不是用户说的话 —— 剥掉；剥完为空整条丢弃（生产 10-18 起 14 个会话被当用户话入库）。
_NUDGE_RE = re.compile(
    r"\[?\s*your previous response had no visible output\.?"
    r"(?:\s*please continue and produce a user-visible response\.?)?\s*\]?",
    re.I)

def user_text(c):
    c = _INJECT_RE.sub("", c)
    c = _NUDGE_RE.sub("", c)
    m = _USER_INPUT_RE.search(c)
    if m:
        blob = m.group(1).strip()
        if not blob:
            return ""
        try:
            j = json.loads(blob)
            if isinstance(j, dict) and j.get("text"):
                return str(j["text"]).strip()
        except Exception:
            pass
        return "" if blob.startswith("<") else blob
    t = c.strip()
    if t.lower().startswith(_NOISE):
        return ""
    return t

msgs = []
try:
    with open(tp, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            m = e.get("message") or {}
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            if e.get("type") not in (None, "user", "assistant"):
                continue
            c = m.get("content")
            if isinstance(c, list):
                if c and all(isinstance(x, dict) and x.get("type") == "tool_result"
                             for x in c):
                    continue
                c = " ".join(x.get("text", "") for x in c
                             if isinstance(x, dict) and x.get("type") == "text")
            if not isinstance(c, str):
                continue
            if role == "user":
                c = user_text(c)
            else:
                c = c.strip()
                if c.lower() in ("no response requested.",
                                 "no response requested"):
                    c = ""
            if not c:
                continue
            msgs.append((role, c[:600]))
except Exception:
    sys.exit()
lu_at = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i][0] == "user"), None)
if lu_at is None:
    sys.exit()
lu = msgs[lu_at][1]
# 回答只取这一问之后的：本轮没有文字回答（只用了工具、被中断、No response requested）
# 时只存问句。此前两者各自取「最后一条」，不看先后，会把上一轮的回答配给这一问。
la = next((c for r, c in reversed(msgs[lu_at + 1:]) if r == "assistant"), None)
messages = [{"role": "user", "content": lu[:500]}]
if la:
    messages.append({"role": "assistant", "content": la[:500]})
prefix = os.environ.get("AIDUMEI_HOOK_PREFIX", "cc-")
print(json.dumps({
    # 幂等加前缀：宿主/重放已带前缀时不再叠加（cc-cc- 双前缀，f0.6+++ WP6）
    "session_id": (sid if sid.startswith(prefix) else prefix + sid) if sid else "",
    "user_id": os.environ.get("AIDUMEI_HOOK_UID", "default"),
    "bank_id": os.environ.get("AIDUMEI_HOOK_BID", "default"),
    "messages": messages,
}, ensure_ascii=False))
' 2>/dev/null)
[ -z "$PARSED" ] && exit 0

# ── 异步沉淀（不阻塞宿主；metadata 口径与萃取线圈定字段一致）──
# 诊断不许随 >/dev/null 扔掉（安静≠失声）：失败行落持久日志，粗粒度轮转。
_log_dir="${AIDUMEM_LOG_DIR:-${HOME:+${HOME}/.aidumem/logs}}"
_log_dir="${_log_dir:-/tmp}"
mkdir -p "$_log_dir" 2>/dev/null || _log_dir="/tmp"
_log="${_log_dir}/aidumem-cc-ingest.log"
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
    "metadata": {"source": "cc-ingest",
                 "_origin_session_id": p.get("session_id", ""),
                 "_origin_agent": "aidumem-cc-ingest"},
}, ensure_ascii=False).encode()
headers = {"Content-Type": "application/json"}
if tok:
    headers["Authorization"] = "Bearer " + tok
req = urllib.request.Request(url + "/add", data=body, headers=headers)
try:
    urllib.request.urlopen(req, timeout=3)
except Exception as exc:
    import sys
    sys.stderr.write("[aidumem-cc-ingest] add_failed err=%s\n" % type(exc).__name__)
' >>"$_log" 2>&1 &
disown 2>/dev/null || true
exit 0

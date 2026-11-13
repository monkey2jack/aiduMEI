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
#   - 绝不拖累宿主：读完 stdin 立即返回，解析与沉淀全在脱离宿主的后台进程里做；任何异常静默 exit 0
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
                  "${_hook_dir:+${_hook_dir}/../../.env}"
    # f0.8+（外审 R3-01）：不读 ./.env——钩子的 cwd 是项目目录，克隆来的仓库能借它改身份、选别人的钥匙
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

# ── f0.8+ P0-1：总开关 / 测试隔离 / 写入白名单（与 cc_memory_bridge.py 同口径）──────
#   AIDUMEI_HOOKS=off          全部钩子静默跳过
#   AIDUMEI_TEST_URL=<url>     测试开关（只认进程环境变量）：只连这个隔离实例；token 只用
#                              AIDUMEI_TEST_TOKEN，不读 .env 里的 token、不读钥匙文件；白名单不适用
#   AIDUMEI_CC_ALLOW_DIRS=a:b  写入白名单（冒号分隔目录）：会话目录（payload cwd，缺省 $PWD）
#                              不在其中时不写；读线只在身份显式配置时才读。未设 = 不限制（老行为）
_hooks=$(printf '%s' "${AIDUMEI_HOOKS:-$(_lookup_env_key AIDUMEI_HOOKS)}" | tr 'A-Z' 'a-z')
case "$_hooks" in off|0|false|no) exit 0 ;; esac
_test_mode=""
if [ -n "${AIDUMEI_TEST_URL:-}" ]; then
    _test_mode=1
    AIDUMEM_URL="${AIDUMEI_TEST_URL%/}"
    AIDUMEM_API_TOKEN="${AIDUMEI_TEST_TOKEN:-}"
fi

# 0 = 会话目录可写（未设白名单 / 测试模式 / 在白名单某目录之下）
_dir_allowed() {
    [ -n "$_test_mode" ] && return 0
    local raw="${AIDUMEI_CC_ALLOW_DIRS:-}" c a ra
    [ -n "$raw" ] || raw=$(_lookup_env_key AIDUMEI_CC_ALLOW_DIRS) || return 0
    c=$(cd -- "${1:-.}" 2>/dev/null && pwd -P) || return 1
    while IFS= read -r a; do
        case "$a" in "~/"*) a="${HOME:-/nonexistent}/${a#\~/}" ;; esac
        case "$a" in /*) ;; *) continue ;; esac
        ra=$(cd -- "$a" 2>/dev/null && pwd -P) || continue
        [ "$ra" = "/" ] && continue
        case "$c/" in "$ra"/*) return 0 ;; esac
    done <<ALLOW_EOF
$(printf '%s' "$raw" | tr ':' '\n')
ALLOW_EOF
    return 1
}

if [ -z "$_test_mode" ] && [ -z "${AIDUMEM_API_TOKEN:-}" ]; then
    AIDUMEM_API_TOKEN=$(_lookup_env_key AIDUMEM_API_TOKEN) || AIDUMEM_API_TOKEN=""
fi
_uid="${AIDUMEM_USER_ID:-${AIDUMEM_DEFAULT_USER_ID:-}}"
if [ -z "$_uid" ]; then
    _uid=$(_lookup_env_key AIDUMEM_USER_ID) \
        || _uid=$(_lookup_env_key AIDUMEM_DEFAULT_USER_ID) \
        || _uid=""
fi
# 身份显式（环境变量或用户级 .env 写明）才算数；兜底的 default 不算
_uid_explicit=1
[ -n "$_uid" ] || { _uid="default"; _uid_explicit=""; }
# f0.7.3：一个 agent 一把钥匙。<钥匙目录>/<身份>.key 在就用它（只在运行时读文件，
# 不进命令行、不进日志）；不在就沿用上面的 AIDUMEM_API_TOKEN。身份名只认安全字符，
# 防「../」读到别的文件。钥匙目录：AIDUMEI_KEYS_DIR，默认 ~/.aidumei-keys。
_kdir="${AIDUMEI_KEYS_DIR:-${HOME:+${HOME}/.aidumei-keys}}"
case "$_uid" in
  ''|.*|*[!A-Za-z0-9._-]*) ;;
  *) if [ -z "$_test_mode" ] && [ -n "$_kdir" ] && [ -f "$_kdir/$_uid.key" ] && [ -r "$_kdir/$_uid.key" ]; then
       _k=$(head -n 1 "$_kdir/$_uid.key" 2>/dev/null | tr -d '\r\n\t ')
       [ -n "$_k" ] && AIDUMEM_API_TOKEN="$_k"
       unset _k
     fi ;;
esac
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

# ── 日志（前台/后台共用）：诊断不许随 >/dev/null 扔掉（安静≠失声），粗粒度轮转 ──
_log_dir="${AIDUMEM_LOG_DIR:-${HOME:+${HOME}/.aidumem/logs}}"
_log_dir="${_log_dir:-/tmp}"
mkdir -p "$_log_dir" 2>/dev/null || _log_dir="/tmp"
_log="${_log_dir}/aidumem-cc-ingest.log"

# ── f0.8+：Stop 钩子立即返回 ─────────────────────────────────────────
# CC 2.1.322 对本钩子报 hook_cancelled：旧版在前台解析整份 transcript（可达几十 MB）再后台发送，
# 宿主等不及就取消（写入其实照常完成，只是日志难看）。现在读完 stdin 就把整件事交给脱离宿主
# 进程组的后台进程（有 setsid 就用），本进程马上 exit 0。后台进程重新解析身份（同一套规则），
# 载荷经环境变量传入（零拼接）。
if [ "${AIDUMEI_CC_HOOK_BG:-}" != "1" ]; then
    RAW_INPUT=$(cat)
    [ -n "$RAW_INPUT" ] || exit 0
    if [ -f "$_log" ] && [ "$(wc -c <"$_log" 2>/dev/null || echo 0)" -gt 2097152 ]; then
        mv -f "$_log" "${_log}.1" 2>/dev/null || :
    fi
    if command -v setsid >/dev/null 2>&1; then
        AIDUMEI_CC_HOOK_BG=1 AIDUMEI_HOOK_INPUT="$RAW_INPUT" \
            setsid bash "${BASH_SOURCE[0]}" </dev/null >>"$_log" 2>&1 &
    else
        AIDUMEI_CC_HOOK_BG=1 AIDUMEI_HOOK_INPUT="$RAW_INPUT" \
            nohup bash "${BASH_SOURCE[0]}" </dev/null >>"$_log" 2>&1 &
    fi
    disown 2>/dev/null || true
    exit 0
fi
RAW_INPUT="${AIDUMEI_HOOK_INPUT:-}"
unset AIDUMEI_HOOK_INPUT AIDUMEI_CC_HOOK_BG

# ── P0-1：会话目录不在写入白名单里 → 不写 ─────────────────────────────
_cwd=$(AIDUMEI_HOOK_INPUT="$RAW_INPUT" python3 -c '
import json, os
try:
    print(str(json.loads(os.environ.get("AIDUMEI_HOOK_INPUT", "")).get("cwd") or ""))
except Exception:
    pass' 2>/dev/null)
if ! _dir_allowed "${_cwd:-$PWD}"; then
    _diag "[aidumem-cc-ingest] skip reason=dir_not_allowed"
    exit 0
fi

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

# ── 沉淀（已在后台；metadata 口径与萃取线圈定字段一致）──
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
import uuid
# One receipt per hook invocation, reused only for its own retry. Identical
# words in later turns must still be stored as separate turns.
headers = {"Content-Type": "application/json",
           "Idempotency-Key": "host-ingest-" + uuid.uuid4().hex}
if tok:
    headers["Authorization"] = "Bearer " + tok
# f0.8+ C: this add runs in the background (never blocks the host), so the old
# 3 s budget only produced TimeoutError under server load. Budget comes from
# AIDUMEI_INGEST_TIMEOUT (default 15 s, clamped 1..60); one retry on a timeout,
# connection error or 5xx (both attempts reuse one idempotency key); a 4xx is final.
# One log line per add that still failed, never per attempt.
import time, urllib.error
try:
    tmo = float(os.environ.get("AIDUMEI_INGEST_TIMEOUT") or 15.0)
except ValueError:
    tmo = 15.0
tmo = min(max(tmo, 1.0), 60.0)
err = None
for attempt in (1, 2):
    req = urllib.request.Request(url + "/add", data=body, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=tmo)
        err = None
        break
    except urllib.error.HTTPError as exc:
        err = "HTTP%d" % exc.code
        if exc.code < 500:
            break
    except Exception as exc:
        err = type(exc).__name__
    if attempt == 1:
        time.sleep(1.0)
if err:
    import sys
    sys.stderr.write("[aidumem-cc-ingest] add_failed err=%s attempts=%d timeout=%.0fs\n" % (err, attempt, tmo))
'
exit 0

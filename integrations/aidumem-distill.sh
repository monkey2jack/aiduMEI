#!/usr/bin/env bash
# aidumem-distill.sh — Hermes Agent `session_end` shell hook
# =====================================================================
# 会话结束时，把「这一程最值得记住的事」提炼成一两句，单独存一条。
#
# **这是第三条线。** 前两条是：
#   读线 aidumem-inject.sh  (pre_llm_call)  —— 把旧记忆喂给模型
#   写线 aidumem-ingest.sh  (post_llm_call) —— 把这一轮存回去
#   本条 aidumem-distill.sh (session_end)   —— 把这一程提炼成一条
#
# 为什么需要它（用户原话）：
#   「那些随口说的一句话、一起解决的一个难题、某个决定的瞬间，现在都淹没在
#     归纳条目里了。」
#   普通写入把每轮拆成若干条语义事实，颗粒是「事实」；精华的颗粒是「这一程」。
#   两者不可互相替代 —— 所以精华单独成条、单独一个慢衰减泳道（distill，0.3），
#   而不是给某条已有记忆加个标记。
#
# 漏挂的代价：比写线轻，但同样安静 —— 记忆照常进，只是永远没有「这一程」
#   那一层。`/health` 的 `distill_liveness_ok` 探针盯着这件事：有会话结束
#   却一条精华都没有，即判降级。
#
# stdin  : Hermes session_end payload (JSON)
# stdout : {}
#
# 配置（与另两条线共用同一套键，故意如此——三条线必须同租户同凭据）：
#   AIDUMEM_URL / AIDUMEM_USER_ID / AIDUMEM_API_TOKEN / AIDUMEM_ENV_FILE …
#   AIDUMEI_DISTILL_TIMEOUT   单次 HTTP 超时秒数，默认 35（要等 LLM 提炼）
#
# 安装：
#   cp integrations/aidumem-distill.sh ~/.hermes/agent-hooks/
#   chmod +x ~/.hermes/agent-hooks/aidumem-distill.sh
#   # config.yaml 注册 hooks.session_end，见 config.yaml.snippet
#   ~/.hermes/agent-hooks/aidumem-distill.sh --selftest
#
# 设计原则（与另两条线同源）：
#   - 绝不拖累宿主：任何异常输出 {} 并 exit 0
#   - 安静≠失声：跳过与失败都往 stderr 写一行结构化诊断
#   - 两步走是有意的：先 /session/distill 只提炼（可安全重跑），再 /add 落库。
#     服务端不内部落库，因为精华必须走 /add 的完整管线才进得了向量库、
#     才能被召回 —— 照反思那样落独立表等于没做。

set -uo pipefail

export AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}"
export AIDUMEI_DISTILL_TIMEOUT="${AIDUMEI_DISTILL_TIMEOUT:-35}"

# ── 凭据与身份（与读线/写线逐行同源）──────────────────────────────
# 三条线必须解析出同一个租户与同一份凭据，否则会出现最难查的一类故障：
# 写进 A 租户、读的是 B 租户、精华又落到 C，三边各自「正常」。
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
                  "${_hook_dir:+${_hook_dir}/../.env}" \
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
export AIDUMEM_API_TOKEN

_uid="${AIDUMEM_USER_ID:-${AIDUMEM_DEFAULT_USER_ID:-}}"
if [ -z "$_uid" ]; then
    _uid=$(_lookup_env_key AIDUMEM_USER_ID) \
        || _uid=$(_lookup_env_key AIDUMEM_DEFAULT_USER_ID) \
        || _uid="default"
fi
export AIDUMEM_USER_ID="$_uid"
export AIDUMEM_HOOK_QUIET="${AIDUMEM_HOOK_QUIET:-}"

# ── 两步：提炼 → 落库 ────────────────────────────────────────────
_distill_and_write() {
    _DISTILL_SESSION="$1" python3 -c '
import json, os, sys, urllib.error, urllib.request

base = os.environ["AIDUMEM_URL"].rstrip("/")
sid = os.environ["_DISTILL_SESSION"]
uid = os.environ["AIDUMEM_USER_ID"]
tmo = float(os.environ["AIDUMEI_DISTILL_TIMEOUT"])
headers = {"Content-Type": "application/json"}
tok = os.environ.get("AIDUMEM_API_TOKEN", "").strip()
if tok:
    headers["Authorization"] = "Bearer " + tok


def _diag(line):
    if not os.environ.get("AIDUMEM_HOOK_QUIET"):
        sys.stderr.write(line)


def _post(path, payload=None, query=""):
    url = base + path + query
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else b"{}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=tmo) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ① 提炼（只读，可安全重跑）
try:
    out = _post("/session/distill",
                query="?session_id=%s&user_id=%s" % (
                    urllib.request.quote(sid), urllib.request.quote(uid)))
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        _diag("[aidumem-distill] auth_failed status=%d token=%s（这一程没有精华）\n"
              % (e.code, "present" if tok else "MISSING"))
    else:
        _diag("[aidumem-distill] http_error status=%d（这一程没有精华）\n" % e.code)
    sys.exit(0)
except Exception as exc:
    _diag("[aidumem-distill] unreachable err=%s（这一程没有精华）\n" % type(exc).__name__)
    sys.exit(0)

status = (out or {}).get("status")
if status == "skipped":
    # 短会话没什么可提炼的 —— 正常，但说出来，别让「这次怎么没精华」查不出。
    _diag("[aidumem-distill] skipped: %s (source=%s min=%s)\n"
          % (out.get("reason"), out.get("source_count"), out.get("min_required")))
    sys.exit(0)
if status != "ok" or not (out or {}).get("summary"):
    _diag("[aidumem-distill] 提炼未产出: %s\n" % json.dumps(out, ensure_ascii=False)[:200])
    sys.exit(0)

# ② 落库（走 /add 完整管线：精华必须进向量库才召回得到）
try:
    _post("/add", {"messages": out["summary"],
                   "user_id": out.get("user_id") or uid,
                   "bank_id": out.get("bank_id") or "default",
                   "async_mode": True,
                   "metadata": out.get("metadata") or {}})
except Exception as exc:
    _diag("[aidumem-distill] 提炼成功但落库失败 err=%s —— 这一程的精华丢了\n"
          % type(exc).__name__)
    sys.exit(0)

if not os.environ.get("AIDUMEM_HOOK_QUIET"):
    sys.stderr.write("[aidumem-distill] ok mode=%s source=%d emotion=%d\n"
                     % (out.get("mode"), out.get("source_count", 0),
                        out.get("emotion_hits", 0)))
'
}

# ── 自检 ─────────────────────────────────────────────────────────
# 必须在 `PAYLOAD=$(cat)` 之前：那一行会阻塞等 stdin。
if [ "${1:-}" = "--selftest" ]; then
    python3 -c '
import json, os, sys, urllib.error, urllib.request
base = os.environ["AIDUMEM_URL"].rstrip("/")
uid = os.environ["AIDUMEM_USER_ID"]
headers = {"Content-Type": "application/json"}
tok = os.environ.get("AIDUMEM_API_TOKEN", "").strip()
if tok:
    headers["Authorization"] = "Bearer " + tok
# 拿一个必然不存在的 session 去打：要的是「端点在、鉴权过、返回结构对」。
# 不造真数据 —— 自检不该往用户库里塞垃圾。
url = base + "/session/distill?session_id=__distill_selftest__&user_id=" + \
    urllib.request.quote(uid)
try:
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        out = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        sys.stderr.write("[aidumem-distill] selftest FAILED auth_failed status=%d user_id=%s\n"
                         "  → 门禁已开启但钩子没拿到有效 token；每次会话结束都会静默无精华。\n"
                         % (e.code, uid))
        sys.exit(3)
    if e.code == 404:
        sys.stderr.write("[aidumem-distill] selftest FAILED 服务端没有 /session/distill\n"
                         "  → 服务端版本过旧，升级到带会话精华的版本再挂本钩子。\n")
        sys.exit(5)
    sys.stderr.write("[aidumem-distill] selftest FAILED http_error status=%d\n" % e.code)
    sys.exit(5)
except Exception as exc:
    sys.stderr.write("[aidumem-distill] selftest FAILED unreachable url=%s err=%s\n" % (base, exc))
    sys.exit(4)
# 不存在的会话应当被判 skipped（source 不足），而不是 error —— 那说明
# 端点活着且判据在工作。
if (out or {}).get("status") not in ("skipped", "ok"):
    sys.stderr.write("[aidumem-distill] selftest FAILED 返回结构异常: %s\n"
                     % json.dumps(out, ensure_ascii=False)[:200])
    sys.exit(6)
print("[aidumem-distill] selftest OK url=%s token=%s user_id=%s （端点在场且判据在工作）"
      % (base, "present" if tok else "absent(门禁未开启)", uid))
'
    exit $?
fi

PAYLOAD=$(cat)

# Hermes session_end payload：session_id 在顶层；非顶层键在 extra 下。
SESSION_ID=$(printf '%s' "$PAYLOAD" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
ex = d.get("extra") or {}
sid = d.get("session_id") or ex.get("session_id") or ""
print(str(sid)[:256])
')

if [ -z "$SESSION_ID" ]; then
    [ -n "${AIDUMEM_HOOK_QUIET:-}" ] || \
        printf '[aidumem-distill] skipped: payload 里没有 session_id，无从圈定这一程\n' >&2
    echo '{}'
    exit 0
fi

_distill_and_write "$SESSION_ID"

echo '{}'
exit 0

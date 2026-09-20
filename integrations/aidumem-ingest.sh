#!/usr/bin/env bash
# aidumem-ingest.sh — Hermes Agent `post_llm_call` shell hook
# =====================================================================
# 把刚刚结束的这一轮对话写回 aiduMEI。**这是「写线」。**
#
# 为什么必须有这个文件（v21.2.0 · 一次真实事故的产物）：
#   在此版本之前，本仓只提供 aidumem-inject.sh（读线），而
#   config.yaml.snippet 与 INTEGRATION_GUIDE.md 也只教人注册 pre_llm_call。
#   于是照着我们文档装的部署，**每轮都在读、从来没写过**。读线漏了几分钟
#   就会被发现（模型明显不记事）；写线漏了，一切看起来都正常——检索有结果
#   （旧记忆还在）、/health 全绿（库里的记忆确实健康）、集成自检通过
#   （那个脚本自己调 /add 自己调 /search，测的是被集成方不是集成本身）。
#   问题只在「新记忆再也没进来过」，而这件事没有任何一个绿灯会变红。
#   教训：读线和写线是一条电路的两端，**装一半等于没装**。
#
# stdin  : Hermes post_llm_call payload (JSON)
# stdout : {} （本 hook 不改写任何东西，只做副作用写入）
#
# 配置（与 aidumem-inject.sh 共用同一套键，故意如此——两条线必须同租户）：
#   AIDUMEM_URL              服务地址，默认 http://127.0.0.1:8767
#   AIDUMEM_USER_ID          记忆归属用户，可由 .env 兜底，默认 default
#   AIDUMEM_DEFAULT_USER_ID  AIDUMEM_USER_ID 缺省时的回落值（服务端同名键）
#   AIDUMEM_API_TOKEN        鉴权门禁 token，可由 .env 兜底
#   AIDUMEM_ENV_FILE / AIDUMEM_HOME   .env 查找入口，同 inject
#   AIDUMEI_INGEST_TIMEOUT   单次 HTTP 超时秒数，默认 6.0（写比读慢，给足）
#   AIDUMEI_INGEST_MIN_CHARS 用户消息短于 N 字不写，默认 8
#   AIDUMEM_HOOK_QUIET       置 1 关闭 stderr 诊断
#
# 安装：
#   cp integrations/aidumem-ingest.sh ~/.hermes/agent-hooks/
#   chmod +x ~/.hermes/agent-hooks/aidumem-ingest.sh
#   # 在 ~/.hermes/config.yaml 注册 hooks.post_llm_call（见 config.yaml.snippet）
#   ~/.hermes/agent-hooks/aidumem-ingest.sh --selftest   # 装完必跑
#
# 自检：
#   `--selftest` 真写一条带自检标记的记忆并回读确认，成功 exit 0，
#   401/403 exit 3，连不上 exit 4，写了但回读不到 exit 6。
#   写线的「静默失败」比读线更毒，所以这条能吵起来的路径是必需品。
#
# 设计原则（与 inject 同源）：
#   - 绝不拖累对话：硬超时 + 任何异常输出 {} 并 exit 0
#   - 安静≠失声：失败时往 stderr 写一行结构化诊断
#   - 短消息不写，省算力也省噪音
#   - **必带 session_id / turn**：服务端靠它做回声抑制与轨迹信用归集。
#     注意 session 不是记忆的作用域——/new 换了 session 也照样搜得到旧记忆，
#     检索是全库的。session 只服务于「同一轮别重复推给你」和「这条记忆
#     后来有没有用」。不传不会报错，只会让这两个能力静默失效。

set -uo pipefail

export AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}"
export AIDUMEI_INGEST_TIMEOUT="${AIDUMEI_INGEST_TIMEOUT:-6.0}"
export AIDUMEI_INGEST_MIN_CHARS="${AIDUMEI_INGEST_MIN_CHARS:-8}"

# ── 凭据与身份 ───────────────────────────────────────────────────
# 与 aidumem-inject.sh 逐行同源。两条线必须解析出同一个租户与同一份凭据，
# 否则会出现最难查的一类故障：写进了 A 租户、读的是 B 租户，两边各自
# 「工作正常」，合起来就是失忆。
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
            printf '%s' "$val"
            return 0
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

# ── 写入实现 ─────────────────────────────────────────────────────
# 入参走环境变量而非命令行，理由见 [shell 陷阱]：参数拼字符串会在含引号、
# 换行、中文的真实对话内容上崩掉，而崩掉的表现又恰好是「静默不写」。
_post_add() {
    python3 -c '
import json, os, sys, urllib.error, urllib.request

url = os.environ["AIDUMEM_URL"].rstrip("/") + "/add"
tok = os.environ.get("AIDUMEM_API_TOKEN", "").strip()
headers = {"Content-Type": "application/json"}
if tok:
    headers["Authorization"] = "Bearer " + tok

def _diag(line):
    if not os.environ.get("AIDUMEM_HOOK_QUIET"):
        sys.stderr.write(line)

body = os.environ["_INGEST_BODY_PIPE"].encode("utf-8")
try:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(
            req, timeout=float(os.environ["AIDUMEI_INGEST_TIMEOUT"])) as resp:
        json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        _diag("[aidumem-ingest] auth_failed status=%d token=%s "
              "(本轮对话没有写进记忆库；请跑 --selftest)\n"
              % (e.code, "present" if tok else "MISSING"))
    else:
        _diag("[aidumem-ingest] http_error status=%d (本轮对话没有写进记忆库)\n"
              % e.code)
    sys.exit(0)
except Exception as exc:
    _diag("[aidumem-ingest] unreachable err=%s (本轮对话没有写进记忆库)\n"
          % type(exc).__name__)
    sys.exit(0)
'
}

# ── 自检模式 ─────────────────────────────────────────────────────
# 必须在 `PAYLOAD=$(cat)` 之前处理：那一行会阻塞等 stdin。
if [ "${1:-}" = "--selftest" ]; then
    python3 -c '
import json, os, sys, time, urllib.error, urllib.request

base = os.environ["AIDUMEM_URL"].rstrip("/")
tok = os.environ.get("AIDUMEM_API_TOKEN", "").strip()
uid = os.environ["AIDUMEM_USER_ID"]
tmo = float(os.environ["AIDUMEI_INGEST_TIMEOUT"])
headers = {"Content-Type": "application/json"}
if tok:
    headers["Authorization"] = "Bearer " + tok

marker = "aidumem ingest hook selftest %d" % int(time.time())
sid = "ingest-selftest-%d" % int(time.time())


def _call(path, payload):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=tmo) as resp:
        return json.loads(resp.read().decode("utf-8"))


try:
    _call("/add", {
        "messages": [{"role": "user", "content": marker}],
        "user_id": uid,
        "async_mode": True,
        "metadata": {"_origin_agent": "aidumem-ingest-selftest",
                     "_origin_session_id": sid,
                     "_origin_turn": 1,
                     "channel": "selftest"},
    })
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        sys.stderr.write(
            "[aidumem-ingest] selftest FAILED auth_failed status=%d url=%s token=%s user_id=%s\n"
            "  → 门禁已开启但 hook 没拿到有效 token；每一轮对话都会静默不写。\n"
            "  → 检查 AIDUMEM_API_TOKEN，或让 .env 对本 hook 的运行用户可读。\n"
            % (e.code, base, "present" if tok else "MISSING", uid))
        sys.exit(3)
    sys.stderr.write("[aidumem-ingest] selftest FAILED http_error status=%d url=%s user_id=%s\n"
                     % (e.code, base, uid))
    sys.exit(5)
except Exception as exc:
    sys.stderr.write("[aidumem-ingest] selftest FAILED unreachable url=%s user_id=%s err=%s\n"
                     % (base, uid, exc))
    sys.exit(4)

# 写完必须回读。只看 /add 返回 200 是不够的：历史上出过「请求收下了、
# 落库没发生」的形态，而那正是本 hook 要防的那类静默。
found = False
for _ in range(8):          # 异步落库，回读要给足耐心
    try:
        res = _call("/search", {"query": marker, "user_id": uid, "caller_user_id": uid,
                                "limit": 5, "metadata": {}})
    except Exception:
        break
    for r in (res.get("results") or []):
        if marker in (r.get("memory") or r.get("text") or ""):
            found = True
            break
    if found:
        break
    time.sleep(1.5)

if not found:
    sys.stderr.write(
        "[aidumem-ingest] selftest FAILED write_not_readable url=%s user_id=%s\n"
        "  → /add 接受了请求，但随后搜不到刚写的内容。\n"
        "  → 可能是异步落库还没完成（稍后重试一次），也可能写入通路本身断了。\n"
        % (base, uid))
    sys.exit(6)

print("[aidumem-ingest] selftest OK url=%s token=%s user_id=%s （写入并回读成功）"
      % (base, "present" if tok else "absent(门禁未开启)", uid))
'
    exit $?
fi

PAYLOAD=$(cat)

# ── 解析 payload ─────────────────────────────────────────────────
# Hermes post_llm_call 的真实形状：非顶层字段一律在 extra 下。
#   {"hook_event_name":"post_llm_call","session_id":...,"cwd":...,
#    "extra":{"user_message":"...","assistant_response":"...","turn_id":N,
#             "conversation_history":[...],"model":"...","platform":"..."}}
# 顶层同名键也认，兼容别的宿主与将来的形状变化。
INGEST_BODY=$(printf '%s' "$PAYLOAD" | _INGEST_UID_PIPE="$AIDUMEM_USER_ID" \
        _INGEST_MIN_PIPE="$AIDUMEI_INGEST_MIN_CHARS" python3 -c '
import json, os, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)

ex = d.get("extra") or {}


def _pick(*names):
    for n in names:
        v = ex.get(n)
        if v:
            return v
        v = d.get(n)
        if v:
            return v
    return ""


user_msg = _pick("user_message", "prompt")
reply = _pick("assistant_response", "response", "completion")


def _skip(why):
    """跳过也要留痕。

    首版这里是裸 `sys.exit(1)` —— 静默跳过。那违反了本文件开头自己写的
    「安静≠失声」：漏写和「按门槛正常跳过」在日志里长得一模一样，于是
    「这一轮怎么没记住」永远查不出是哪种。用户审计当场点名（🔴-1）。
    """
    if not os.environ.get("AIDUMEM_HOOK_QUIET"):
        sys.stderr.write("[aidumem-ingest] skipped: %s\n" % why)
    sys.exit(1)


if not isinstance(user_msg, str) or not isinstance(reply, str):
    _skip("payload 里取不到 user_message/assistant_response 的文本形态")
if len(user_msg.strip()) < int(os.environ["_INGEST_MIN_PIPE"]):
    _skip("user_message 只有 %d 字，低于门槛 %s（正常跳过，非故障）"
          % (len(user_msg.strip()), os.environ["_INGEST_MIN_PIPE"]))

# 机器输出占压倒多数的轮次不进语义层。
# 由来（用户审计 🔵-4）：写线接上后，贴满 terminal 输出与脚本全文的轮次会
# 被 LLM 抽取成「记忆」，后续搜「生日」可能召回一堆 bash 片段。判据**故意
# 保守** —— 只拦极端情况（几乎整轮都是命令行/代码围栏），宁可放进去一些
# 噪音，也不能把真内容judged成日志丢掉。跳过同样留痕，不静默。
def _machine_ratio(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 12:                 # 短内容一律不判，样本不足
        return 0.0
    marks = ("$ ", "# ", "  File \"", "Traceback", "    at ", "```",
             "+++", "---", "|  ", "INFO ", "DEBUG ", "WARNING ", "ERROR ")
    hit = sum(1 for ln in lines if ln.startswith(marks) or "\t" in ln)
    return hit / len(lines)


_combined = user_msg + "\n" + reply
_ratio = _machine_ratio(_combined)
if _ratio >= 0.8:
    _skip("整轮 %.0f%% 是命令行/日志输出，不进语义层（避免污染召回）" % (_ratio * 100))

session_id = str(_pick("session_id", "conversation_id") or "")

# turn 必须是**序号**，不是 id。
# 宿主传的 turn_id 是字符串（Hermes 侧 `turn_id = str(...)`），首版直接
# int() 它，必然 ValueError 落到 0 —— 实测生产真实写入的 origin_turn 全是 0，
# M1 轨迹信用要的「这条记忆在轨迹里第几步」因此恒拿不到。
# 改用本会话已有的 user 轮数：有序、单调、就在 payload 里现成有。
_hist = _pick("conversation_history", "messages") or []
turn = 0
if isinstance(_hist, list):
    turn = sum(1 for m in _hist
               if isinstance(m, dict) and m.get("role") == "user")
if turn <= 0:
    # 退而求其次：turn_id 本身是数字形态时才用它，否则如实留 0（不编造）
    _tid = _pick("turn_id", "turn")
    if isinstance(_tid, int):
        turn = _tid
    elif isinstance(_tid, str) and _tid.strip().lstrip("-").isdigit():
        turn = int(_tid.strip())
agent = str(_pick("platform", "agent", "model") or "hermes")

# 单条上限 50_000 字符、整体序列化 64 KiB（服务端 AddRequest 的硬约束）。
# 超了服务端会 422，而 422 在本 hook 里只会变成一行 stderr——也就是
# 「长对话静默不写」。所以在这里先裁，裁的位置和理由都要看得见。
LIMIT = 20_000
messages = [
    {"role": "user", "content": user_msg[:LIMIT]},
    {"role": "assistant", "content": reply[:LIMIT]},
]

print(json.dumps({
    "messages": messages,
    "user_id": os.environ["_INGEST_UID_PIPE"],
    # async_mode 不是优化，是正确性：同步 /add 要跑完整抽取管线（生产实测
    # p50 约 4 秒，长尾未知），而宿主给 hook 的超时通常是个位数秒。超时被杀
    # 的钩子 = 静默不写，正是本文件要防的那件事。异步下服务端先收下再后台
    # 落库，溯源三件套在入口就已归一进 metadata，不受影响。
    "async_mode": True,
    "metadata": {
        "_origin_agent": agent[:256],
        "_origin_session_id": session_id[:256],
        "_origin_turn": turn,
        "channel": "hermes-post-llm",
    },
}, ensure_ascii=False))
') || INGEST_BODY=""
# 这一行故意**不带** 2>/dev/null：解析段的 stderr 是诊断通路，扔掉它等于把
# 「安静≠失声」原则作废。首版扔了，于是 _skip() 写的每一行都进了黑洞 ——
# 用户审计报「短消息跳过静默无痕」时，脚本里其实是写了的，被我自己捂住了。

if [ -z "$INGEST_BODY" ]; then
    echo '{}'
    exit 0
fi

_INGEST_BODY_PIPE="$INGEST_BODY" _post_add

echo '{}'
exit 0

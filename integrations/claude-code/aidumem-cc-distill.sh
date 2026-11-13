#!/usr/bin/env bash
# aidumem-cc-distill.sh — Claude Code `SessionEnd` shell hook（萃取线薄适配层）
# =====================================================================
# 会话结束时，把「这一程最值得记住的事」提炼成一两条，单独存一条精华。
#
# ══ 设计：薄适配层，不重造萃取逻辑 ══
# 提炼的真源是仓内久经生产验证的 integrations/aidumem-distill.sh
# （两步走：/session/distill 只提炼 → /add 落库；稳定幂等键防重放、
# 后台 setsid 不阻塞宿主、同会话 flock 互斥、日志轮转、skip_platform）。
# 本适配层只做两件 cc↔Hermes 的口径转换：
#   ① payload 形状：cc 的 {session_id, hook_event_name:SessionEnd}
#      → distill 真源认的 {session_id, hook_event_name:on_session_end}
#   ② session_id 前缀：${AIDUMEI_CC_SESSION_PREFIX}<sid>（默认 cc-），
#      与写线 aidumem-cc-ingest.sh 沉淀的 _origin_session_id 逐字一致 ——
#      差一个字符，distill_session 就圈不到 verbatim 来源，精华线继续断着
#      （f0.4++ 根因修复前的实测病灶：误调 /session/start，同一会话反复
#      「Session 创建」、提炼从未发生）。
# 萃取逻辑只有一份真源；真源升级，cc 侧自动受益。
#
# stdin  : Claude Code SessionEnd payload (JSON: session_id/...)
# stdout : （无）
#
# 配置（与读线/写线共用同一套键，三线必须同租户同凭据）：
#   AIDUMEM_URL / AIDUMEM_USER_ID / AIDUMEM_DEFAULT_USER_ID / AIDUMEI_BANK_ID
#   AIDUMEM_API_TOKEN / AIDUMEM_ENV_FILE / AIDUMEM_LOG_DIR
#   AIDUMEI_CC_SESSION_PREFIX  会话 id 前缀，默认 cc-（与写线必须同值）
#   AIDUMEI_INSTALL_DIR        aiduMEI 安装目录（找真源用；缺省按本脚本
#                              位置推导：integrations/claude-code/ 的上上级）
set -uo pipefail
# f0.7 P3：新前缀别名（connect_host 写 AIDUMEI_URL；AIDUMEM_URL 设了仍优先）
if [ -z "${AIDUMEM_URL:-}" ] && [ -n "${AIDUMEI_URL:-}" ]; then export AIDUMEM_URL="$AIDUMEI_URL"; fi

# ── .env 链（与读线/写线逐行同源；f0.8+ R3-01 起不读 ./.env）+ P0-1 开关与白名单 ──
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

INSTALL_DIR="${AIDUMEI_INSTALL_DIR:-$(cd -- "${_hook_dir:+$_hook_dir/../..}" 2>/dev/null && pwd)}"
DISTILL_SRC="$INSTALL_DIR/integrations/aidumem-distill.sh"
SESSION_PREFIX="${AIDUMEI_CC_SESSION_PREFIX-cc-}"

# 真源不在（非标准部署）→ 静默放行，绝不阻塞宿主退出。
# 宁可这一程没精华，也不能卡住会话；诊断写 stderr 供排查。
if [ ! -f "$DISTILL_SRC" ]; then
  echo "[aidumem-cc-distill] distill 真源不在 $DISTILL_SRC，本程跳过萃取" >&2
  exit 0
fi

RAW_INPUT=$(cat)

# cc payload → distill 真源认的口径（环境变量传递，零字符串拼接）
HERMES_PAYLOAD=$(AIDUMEI_HOOK_INPUT="$RAW_INPUT" AIDUMEI_HOOK_PREFIX="$SESSION_PREFIX" \
  python3 -c '
import json, os, sys
try:
    d = json.loads(os.environ.get("AIDUMEI_HOOK_INPUT", ""))
except Exception:
    sys.exit()
sid = str(d.get("session_id") or "")[:256]
if not sid:
    sys.exit()
prefix = os.environ.get("AIDUMEI_HOOK_PREFIX", "cc-")
# cc SessionEnd 是真实会话边界，对应 on_session_end；宿主如提供
# finalize/reset 语义可在此映射（真源对二者会加 --force 出新一代）。
# 幂等加前缀：已带前缀不再叠加（cc-cc- 双前缀，f0.6+++ WP6），与写线同口径
print(json.dumps({"session_id": sid if sid.startswith(prefix) else prefix + sid,
                  "hook_event_name": "on_session_end"}, ensure_ascii=False))
' 2>/dev/null)
[ -z "$HERMES_PAYLOAD" ] && exit 0

# P0-1：会话目录不在写入白名单里 → 不提炼不落库
_cwd=$(AIDUMEI_HOOK_INPUT="$RAW_INPUT" python3 -c '
import json, os
try:
    print(str(json.loads(os.environ.get("AIDUMEI_HOOK_INPUT", "")).get("cwd") or ""))
except Exception:
    pass' 2>/dev/null)
if ! _dir_allowed "${_cwd:-$PWD}"; then
  echo "[aidumem-cc-distill] skip reason=dir_not_allowed" >&2
  exit 0
fi

# 交给真源跑（后台化由真源内部的 setsid nohup 负责，本层立即返回）。
# 三线同租户同凭据：把本层解析出的配置原样透传。
printf '%s' "$HERMES_PAYLOAD" | \
  AIDUMEM_ENV_FILE="${AIDUMEM_ENV_FILE:-}" \
  AIDUMEM_API_TOKEN="${AIDUMEM_API_TOKEN:-}" \
  AIDUMEI_TEST_URL="${AIDUMEI_TEST_URL:-}" AIDUMEI_TEST_TOKEN="${AIDUMEI_TEST_TOKEN:-}" \
  AIDUMEM_USER_ID="${AIDUMEM_USER_ID:-${AIDUMEM_DEFAULT_USER_ID:-}}" \
  AIDUMEI_BANK_ID="${AIDUMEI_BANK_ID:-}" \
  AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}" \
  AIDUMEM_LOG_DIR="${AIDUMEM_LOG_DIR:-${HOME:+${HOME}/.aidumem/logs}}" \
  bash "$DISTILL_SRC" >/dev/null 2>&1
exit 0

#!/usr/bin/env bash
# aidumem-codex-distill.sh — Codex CLI `SessionEnd` hook（萃取线薄适配层）
# =====================================================================
# 会话结束时，把「这一程最值得记住的事」提炼成一两条，单独存一条精华。
#
# ══ 设计：薄适配层，不重造萃取逻辑 ══
# 提炼的真源是仓内久经生产验证的 integrations/aidumem-distill.sh
# （两步走：/session/distill 只提炼 → /add 落库；稳定幂等键防重放、
# 后台 setsid 不阻塞宿主、同会话 flock 互斥、日志轮转、skip_platform）。
# 本适配层只做两件 Codex↔Hermes 的口径转换：
#   ① payload 形状：Codex SessionEnd 的 {session_id, reason:"other"}
#      → distill 真源认的 {session_id, hook_event_name:on_session_end}
#   ② session_id 前缀：${AIDUMEI_CODEX_SESSION_PREFIX}<sid>（默认 codex-），
#      与写线 aidumem-codex-ingest.sh 沉淀的 _origin_session_id 逐字一致 ——
#      差一个字符，distill_session 就圈不到 verbatim 来源，精华线继续断着。
# 萃取逻辑只有一份真源；真源升级，Codex 侧自动受益。
#
# ⏱ 超时铁律：Codex SessionEnd hook 默认 1s、上限 3s，且**始终同步执行**
#   （async:true 对本事件无效）。真源内部已用 setsid nohup 后台化并立即
#   返回，本层只负责口径转换 + 透传，绝不在此等待 LLM 提炼。
#
# stdin  : Codex SessionEnd payload (JSON: session_id/transcript_path/cwd/reason)
# stdout : （无 —— SessionEnd 是 advisory，输出不会改变 Codex 行为）
#
# 配置（与读线/写线共用同一套键，三线必须同租户同凭据）：
#   AIDUMEM_URL / AIDUMEM_USER_ID / AIDUMEM_DEFAULT_USER_ID / AIDUMEI_BANK_ID
#   AIDUMEM_API_TOKEN / AIDUMEM_ENV_FILE / AIDUMEM_LOG_DIR
#   AIDUMEI_CODEX_SESSION_PREFIX  会话 id 前缀，默认 codex-（与写线必须同值）
#   AIDUMEI_INSTALL_DIR        aiduMEI 安装目录（找真源用；缺省按本脚本
#                              位置推导：integrations/codex/ 的上上级）
set -uo pipefail
# f0.7 P3：新前缀别名（connect_host 写 AIDUMEI_URL；AIDUMEM_URL 设了仍优先）
if [ -z "${AIDUMEM_URL:-}" ] && [ -n "${AIDUMEI_URL:-}" ]; then export AIDUMEM_URL="$AIDUMEI_URL"; fi

_hook_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd) || _hook_dir=""
INSTALL_DIR="${AIDUMEI_INSTALL_DIR:-$(cd -- "${_hook_dir:+$_hook_dir/../..}" 2>/dev/null && pwd)}"
DISTILL_SRC="$INSTALL_DIR/integrations/aidumem-distill.sh"
SESSION_PREFIX="${AIDUMEI_CODEX_SESSION_PREFIX-codex-}"

# 真源不在（非标准部署）→ 静默放行，绝不阻塞宿主退出。
# 宁可这一程没精华，也不能卡住会话；诊断写 stderr 供排查。
if [ ! -f "$DISTILL_SRC" ]; then
  echo "[aidumem-codex-distill] distill 真源不在 $DISTILL_SRC，本程跳过萃取" >&2
  exit 0
fi

RAW_INPUT=$(cat)

# Codex payload → distill 真源认的口径（环境变量传递，零字符串拼接）
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
prefix = os.environ.get("AIDUMEI_HOOK_PREFIX", "codex-")
# Codex SessionEnd 是真实会话边界（归档/删除/正常关闭/闲置 30 分钟），
# 对应 on_session_end；真源对 finalize/reset 会加 --force 出新一代，
# Codex 暂无该语义区分，统一按 on_session_end 处理。
# 幂等加前缀：已带前缀不再叠加（cc-cc- 双前缀，f0.6+++ WP6），与写线同口径
print(json.dumps({"session_id": sid if sid.startswith(prefix) else prefix + sid,
                  "hook_event_name": "on_session_end"}, ensure_ascii=False))
' 2>/dev/null)
[ -z "$HERMES_PAYLOAD" ] && exit 0

# 交给真源跑（后台化由真源内部的 setsid nohup 负责，本层立即返回）。
# 三线同租户同凭据：把本层解析出的配置原样透传。
printf '%s' "$HERMES_PAYLOAD" | \
  AIDUMEM_ENV_FILE="${AIDUMEM_ENV_FILE:-}" \
  AIDUMEM_API_TOKEN="${AIDUMEM_API_TOKEN:-}" \
  AIDUMEM_USER_ID="${AIDUMEM_USER_ID:-${AIDUMEM_DEFAULT_USER_ID:-}}" \
  AIDUMEI_BANK_ID="${AIDUMEI_BANK_ID:-}" \
  AIDUMEM_URL="${AIDUMEM_URL:-http://127.0.0.1:8767}" \
  AIDUMEM_LOG_DIR="${AIDUMEM_LOG_DIR:-${HOME:+${HOME}/.aidumem/logs}}" \
  bash "$DISTILL_SRC" >/dev/null 2>&1
exit 0

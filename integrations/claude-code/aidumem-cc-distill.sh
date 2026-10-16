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

_hook_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd) || _hook_dir=""
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
print(json.dumps({"session_id": prefix + sid,
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

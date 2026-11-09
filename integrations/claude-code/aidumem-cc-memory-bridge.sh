#!/usr/bin/env bash
# aidumem-cc-memory-bridge.sh — Claude Code `PostToolUse` shell hook（自带记忆桥，f0.8 ①）
# =====================================================================
# matcher: Write|Edit|MultiEdit。被改的文件在本项目 CC 记忆目录
# （~/.claude/projects/<项目>/memory/）里时：
#   1. 用 aiduMEI 原则 7 打码器扫一遍，命中就原地改写（原子替换，保留权限/属主）；
#   2. 主题文件正文经鉴权 /add 写进当前身份（服务端写入闸门照常生效），
#      同内容不重复写，文件更新后取代早先那次导入。
# 逻辑全在同目录 cc_memory_bridge.py（只用标准库）；本壳只负责找到它。
#
# stdin  : Claude Code PostToolUse payload (JSON)
# stdout : 有打码时输出一段 hookSpecificOutput.additionalContext 提醒模型；否则无
# 配置   : 与读线/写线同一套键（AIDUMEM_URL / AIDUMEM_USER_ID / AIDUMEI_BANK_ID /
#          AIDUMEM_API_TOKEN / AIDUMEM_ENV_FILE / AIDUMEI_KEYS_DIR）
#          AIDUMEI_CC_MEMORY_DIR      记忆目录覆盖（缺省按 transcript/cwd 推）
#          AIDUMEI_CC_BRIDGE_INGEST=0 只打码不入库
#          AIDUMEI_CC_BRIDGE_STATE    去重状态文件（缺省 ~/.aidumem/cc-bridge-state.json）
#          AIDUMEI_CC_BRIDGE_TIMEOUT  每个 HTTP 请求超时秒数（缺省 12；钩子总超时 30）
# 设计原则：绝不拖累宿主——任何异常 exit 0。
set -uo pipefail
if [ -z "${AIDUMEM_URL:-}" ] && [ -n "${AIDUMEI_URL:-}" ]; then export AIDUMEM_URL="$AIDUMEI_URL"; fi
_hook_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd) || exit 0
_py="$_hook_dir/cc_memory_bridge.py"
[ -f "$_py" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
python3 "$_py" hook 2>/dev/null || true
exit 0

#!/bin/bash
# update_crontab.sh — 把 aiduMEM 的周期维护任务追加到 crontab
# 保留现有条目，只做幂等追加
# 用法: bash scripts/update_crontab.sh [--dry-run]

set -euo pipefail

# 仓库根自动解析（本文件位于 <repo>/scripts/），可用 AIDUMEM_HOME 覆盖
REPO_ROOT="${AIDUMEM_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${AIDUMEM_PYTHON:-${REPO_ROOT}/venv/bin/python3}"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"

BACKUP_FILE="${LOG_DIR}/crontab_backup_$(date +%Y%m%d_%H%M%S).txt"

# 记忆巩固：每天 04:00 跑一次（衰减 + 矛盾检测 + 指标）
NEW_ENTRIES=$(cat <<CRON
# aiduMEI: 每日记忆巩固（Salience 衰减 / 矛盾检测 / 每日指标）
0 4 * * * cd ${REPO_ROOT} && ${PY} scripts/consolidator.py >> ${LOG_DIR}/consolidator.log 2>&1
CRON
)

DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=true
    echo "[DRY-RUN] 以下将被追加到 crontab:"
    echo "$NEW_ENTRIES"
    echo ""
    echo "当前 crontab:"
    crontab -l 2>/dev/null || echo "(empty)"
    exit 0
fi

# 备份当前 crontab
crontab -l > "$BACKUP_FILE" 2>/dev/null || true
echo "已备份 crontab 到: $BACKUP_FILE"

# 幂等检查：已存在就不重复追加
CURRENT=$(crontab -l 2>/dev/null || true)
if echo "$CURRENT" | grep -q "scripts/consolidator.py"; then
    echo "consolidator.py 已存在于 crontab，跳过添加"
else
    (crontab -l 2>/dev/null; echo ""; echo "$NEW_ENTRIES") | crontab -
    echo "已添加 aiduMEI 维护条目"
fi

echo ""
echo "当前 crontab:"
crontab -l 2>/dev/null || echo "(empty)"

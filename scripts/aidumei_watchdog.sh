#!/bin/bash
# aiduMEI 守护探针（f0.4++ WP2 · cron 每分钟一跳）
#
# 职责（只做三件事，绝不多手）：
#   1. qdrant 容器不在 → docker start 拉起
#   2. API /health 不通 → 用守护脚本拉起（cd 铁律：必须从安装目录启动）
#   3. 每次拉起动作写日志留痕（含时间、原因、结果），供结案与巡检回读
#
# 铁律对齐：
#   - 静默失败铁律（SOP 铁律 8）：服务死了必须有人知道 → 本脚本 + 日志
#   - 生产数据第一天条：本脚本永不碰 data/，只探活与拉起进程
#   - 绝不盲杀：只拉起，不 kill 任何东西；进程僵死的处置留给人工裁决
#
# crontab 安装（宿主机；路径按实际安装目录替换）：
#   * * * * * <安装目录>/scripts/aidumei_watchdog.sh >> <安装目录>/logs/watchdog.log 2>&1
set -o pipefail

# 安装目录缺省按脚本自身位置推导（scripts/ 的上上级），部署零配置。
_script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd) || _script_dir=""
INSTALL_DIR="${AIDUMEI_INSTALL_DIR:-$(cd -- "${_script_dir:+$_script_dir/..}" 2>/dev/null && pwd)}"
API_URL="${AIDUMEI_URL:-http://127.0.0.1:8767}"
QDRANT_CONTAINER="${AIDUMEI_QDRANT_CONTAINER:-qdrant-server}"
GUARD_LOG="$INSTALL_DIR/logs/watchdog.log"
START_SCRIPT="$INSTALL_DIR/start-aidumei.sh"

ts() { date '+%F %T'; }

mkdir -p "$INSTALL_DIR/logs"

# ── 1. qdrant 容器守卫 ──────────────────────────────────
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$QDRANT_CONTAINER"; then
  : # 容器活着，不动
else
  echo "$(ts) [watchdog] qdrant 容器 $QDRANT_CONTAINER 不在运行，尝试拉起" >> "$GUARD_LOG"
  if docker start "$QDRANT_CONTAINER" >> "$GUARD_LOG" 2>&1; then
    echo "$(ts) [watchdog] ✅ qdrant 已拉起" >> "$GUARD_LOG"
    sleep 3  # 给 qdrant 一点就绪时间再探 API
  else
    echo "$(ts) [watchdog] ❌ qdrant 拉起失败（容器可能不存在，人工核查）" >> "$GUARD_LOG"
  fi
fi

# ── 2. API 健康守卫 ─────────────────────────────────────
# 探活给足 5s：服务启动预热期（mem0 单例、qdrant 连接）响应偏慢，
# 0.3s 探活在高峰期会误判（2026-10-16 19:10 熔断实证）。
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$API_URL/health" 2>/dev/null)

if [ "$HTTP_CODE" = "200" ]; then
  exit 0  # 健康，安静退场（正常心跳不刷日志，防日志膨胀）
fi

echo "$(ts) [watchdog] ⚠️ /health 异常（http_code=$HTTP_CODE），检查进程" >> "$GUARD_LOG"

# 进程还在但 /health 不通（僵死/预热中）→ 不盲杀，留痕后等下一跳
if pgrep -f "$INSTALL_DIR/venv/bin/python api_server.py" >/dev/null 2>&1; then
  echo "$(ts) [watchdog] api_server 进程存在但 /health 未就绪（预热或僵死），本跳不处置，连续 5 跳未愈请人工介入" >> "$GUARD_LOG"
  # 连续未愈计数器：5 跳（约 5 分钟）后升级为显著告警行
  CNT_FILE="/tmp/.aidumei_watchdog_unhealthy_count"
  CNT=$(( $(cat "$CNT_FILE" 2>/dev/null || echo 0) + 1 ))
  echo "$CNT" > "$CNT_FILE"
  if [ "$CNT" -ge 5 ]; then
    echo "$(ts) [watchdog] 🔴 连续 $CNT 跳 /health 未就绪且进程仍在——疑似僵死，需人工裁决（本脚本绝不盲杀）" >> "$GUARD_LOG"
  fi
  exit 0
fi

# 进程不在 → 拉起（cd 铁律在 start-aidumei.sh 内保证）
rm -f /tmp/.aidumei_watchdog_unhealthy_count
if [ ! -x "$START_SCRIPT" ] && [ ! -f "$START_SCRIPT" ]; then
  echo "$(ts) [watchdog] ❌ 守护启动脚本不存在: $START_SCRIPT" >> "$GUARD_LOG"
  exit 1
fi
echo "$(ts) [watchdog] api_server 进程不存在，用 $START_SCRIPT 拉起" >> "$GUARD_LOG"
nohup bash "$START_SCRIPT" &>> "$INSTALL_DIR/logs/aidumem.log" &
disown

# 拉起后复核（最多等 30s，服务冷启动含 mem0 预热）
for i in $(seq 1 10); do
  sleep 3
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$API_URL/health" 2>/dev/null)
  if [ "$CODE" = "200" ]; then
    echo "$(ts) [watchdog] ✅ api_server 已复活（第 $i 次复核，约 $((i*3))s）" >> "$GUARD_LOG"
    exit 0
  fi
done
echo "$(ts) [watchdog] 🔴 拉起后 30s /health 仍未就绪（last_code=$CODE）——查 logs/aidumem.log，人工介入" >> "$GUARD_LOG"
exit 1

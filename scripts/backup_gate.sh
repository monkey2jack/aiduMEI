#!/bin/bash
# ============================================================================
# aiduMEM 备份门禁 backup_gate.sh（v19.4.0 · Mímir 借鉴 B2 备份纪律）
# 2026-08-17
#
# 借鉴 Mímir §17.2：每次 schema / 数据结构变更前必有 pre-migration 备份
# + sha256 校验和 + 完整性校验；无备份则迁移脚本拒绝执行（硬门禁，不靠自觉）。
#
# 用法:
#   bash scripts/backup_gate.sh create <label>   迁移前备份 + sha256 + quick_check
#   bash scripts/backup_gate.sh verify <dir>     复验某份备份（sha256 + quick_check）
#   bash scripts/backup_gate.sh require          硬门禁：无已验证备份则 exit 1
#
# 环境变量:
#   AIDUMEM_DATA_DIR     要备份的数据目录（默认 <repo>/data）
#   AIDUMEM_BACKUP_ROOT  备份根目录（默认 /root/aidumei_backups）
#
# 铁律:
#   - 备份只进持久目录，落在 /tmp 下一律拒绝（v19.4.0 审计 🔴-1 教训）
#   - 迁移前备份永久保留；每日份保留策略由 cron 侧控制
# ============================================================================

set -euo pipefail

REPO_ROOT="${AIDUMEM_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${AIDUMEM_DATA_DIR:-${REPO_ROOT}/data}"
BACKUP_ROOT="${AIDUMEM_BACKUP_ROOT:-/root/aidumei_backups}"

if [[ -t 1 ]]; then
  GREEN='\033[0;32m'; RED='\033[0;31m'; RESET='\033[0m'
else
  GREEN=''; RED=''; RESET=''
fi
ok()  { echo -e "  ${GREEN}✅${RESET} $1"; }
bad() { echo -e "  ${RED}❌${RESET} $1" >&2; }

# SQLite 完整性校验（python3 兜底，不依赖 sqlite3 CLI）
quick_check() {
  local db="$1"
  python3 - "$db" <<'PYEOF'
import sqlite3, sys
db = sys.argv[1]
try:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    r = conn.execute("PRAGMA quick_check").fetchone()[0]
    conn.close()
    sys.exit(0 if r == "ok" else 1)
except Exception as e:
    print(f"quick_check failed: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
}

# 拒绝 /tmp 系备份根（重启即灰飞烟灭）
assert_persistent_root() {
  case "$BACKUP_ROOT" in
    /tmp/*|/var/tmp/*|/private/tmp/*)
      bad "备份根目录 ${BACKUP_ROOT} 在 /tmp 系——铁律拒绝（备份只进持久目录）"
      exit 1
      ;;
  esac
}

cmd_create() {
  local label="${1:-migration}"
  assert_persistent_root
  if [[ ! -d "$DATA_DIR" ]]; then
    bad "数据目录不存在: $DATA_DIR"
    exit 1
  fi
  mkdir -p "$BACKUP_ROOT"
  local ts dest
  ts="$(date +%Y%m%d_%H%M%S)"
  dest="${BACKUP_ROOT}/pre-${label}-${ts}"
  echo "📦 备份 ${DATA_DIR} → ${dest}"
  cp -a "$DATA_DIR" "$dest"

  echo "🔐 生成 sha256 校验和"
  ( cd "$dest" && find . -type f ! -name SHA256SUMS ! -name .backup_verified \
      -exec sh -c 'sha256sum "$1" 2>/dev/null || shasum -a 256 "$1"' _ {} \; > SHA256SUMS )

  echo "🩺 SQLite quick_check"
  local db
  for db in "$dest"/*.db; do
    [[ -f "$db" ]] || continue
    if quick_check "$db"; then
      ok "quick_check $(basename "$db")"
    else
      bad "quick_check 失败: $db（备份不可信，已删除）"
      rm -rf "$dest"
      exit 1
    fi
  done

  # 验证标记：require 模式只认带此标记且 sha256 可复验的备份
  echo "label=${label}" >  "$dest/.backup_verified"
  echo "created_at=${ts}" >> "$dest/.backup_verified"
  ok "备份完成并通过校验: ${dest}"
  echo "$dest"
}

cmd_verify() {
  local dest="$1"
  if [[ ! -d "$dest" ]]; then
    bad "备份目录不存在: $dest"
    exit 1
  fi
  echo "🔐 复验 sha256: $dest"
  if ( cd "$dest" && (sha256sum -c SHA256SUMS >/dev/null 2>&1 || shasum -a 256 -c SHA256SUMS >/dev/null 2>&1) ); then
    ok "sha256 全部匹配"
  else
    bad "sha256 校验失败"
    exit 1
  fi
  local db
  for db in "$dest"/*.db; do
    [[ -f "$db" ]] || continue
    quick_check "$db" || { bad "quick_check 失败: $db"; exit 1; }
  done
  ok "备份复验通过: $dest"
}

cmd_require() {
  assert_persistent_root
  if [[ ! -d "$BACKUP_ROOT" ]]; then
    bad "硬门禁：备份根目录不存在（$BACKUP_ROOT）——先跑 backup_gate.sh create 再迁移"
    exit 1
  fi
  local d
  for d in "$BACKUP_ROOT"/pre-*/; do
    [[ -d "$d" ]] || continue
    if [[ -f "$d/.backup_verified" && -f "$d/SHA256SUMS" ]]; then
      if ( cd "$d" && (sha256sum -c SHA256SUMS >/dev/null 2>&1 || shasum -a 256 -c SHA256SUMS >/dev/null 2>&1) ); then
        ok "硬门禁放行：存在已验证备份 ${d%/}"
        exit 0
      fi
    fi
  done
  bad "硬门禁：${BACKUP_ROOT} 下没有任何通过 sha256 验证的迁移前备份——拒绝迁移"
  exit 1
}

case "${1:-}" in
  create)  shift; cmd_create "${1:-migration}" ;;
  verify)  shift; cmd_verify "${1:?用法: backup_gate.sh verify <备份目录>}" ;;
  require) cmd_require ;;
  *)
    echo "用法: backup_gate.sh {create <label> | verify <dir> | require}"
    exit 2
    ;;
esac

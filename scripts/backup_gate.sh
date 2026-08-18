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

# ── 一致性快照 + 完整性校验（v19.4.1 修复）─────────────────────────────
#
# 为什么不能直接 cp WAL 库（v19.4.1 生产实机暴露的真问题）：
#   生产库跑在 journal_mode=WAL 下，目录里有 facts.db / facts.db-wal /
#   facts.db-shm 三个文件。此前 cmd_create 的顺序是：
#     ① cp -a 整个 data 目录（连 -wal/-shm 一起拷）
#     ② 对所有文件生成 SHA256SUMS
#     ③ 再对每个 .db 跑 quick_check
#   第 ③ 步打开数据库会重建 -shm 并把 -wal 的页 checkpoint 进主库 ——
#   第 ② 步刚算好的 facts.db 与 facts.db-shm 校验和当场失效。
#   实机复现：create 报「备份完成并通过校验」，紧接着 require 却判
#   「没有任何通过 sha256 验证的备份——拒绝迁移」，4 个 -shm 全部 FAILED。
#   结果是 B2 备份纪律形同虚设：门禁永远拦，运维只会学会绕过它。
#
# 正确做法：用 SQLite 在线备份 API 生成**已合并 WAL 的单文件一致快照**，
# 备份目录不留 -wal/-shm，校验和从此稳定；快照落盘后再算 sha256。
snapshot_db() {
  local src="$1" dst="$2"
  python3 - "$src" "$dst" <<'PYEOF'
import os
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
try:
    # 源库以读写方式打开：在线备份 API 需要读锁，WAL 库还需能访问 -shm。
    # 备份本身不修改源库数据（只可能推进 WAL checkpoint，语义无损）。
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)          # 一致性快照，自动合并 WAL
    # 快照转 DELETE 日志模式：备份件不再需要 -wal/-shm 伴生文件，
    # 之后 quick_check 能纯只读打开，sha256 基线不会被打开动作改写。
    d.execute("PRAGMA journal_mode=DELETE")
    r = d.execute("PRAGMA quick_check").fetchone()[0]
    d.close()
    s.close()
    if r != "ok":
        print(f"quick_check 不通过: {r}", file=sys.stderr)
        sys.exit(1)
    # 显式清掉快照的伴生文件：转 DELETE 模式后它们已无意义，
    # 但 SQLite 不保证关闭时一定删除 -shm。留着会让 verify 阶段的
    # quick_check 重建它、从而打废 SHA256SUMS 基线（本次要根治的正是这点）。
    for suffix in ("-wal", "-shm", "-journal"):
        side = dst + suffix
        if os.path.exists(side):
            try:
                os.remove(side)
            except OSError as oe:
                print(f"清理伴生文件 {side} 失败（不致命）: {oe}", file=sys.stderr)
    sys.exit(0)
except Exception as e:
    print(f"snapshot 失败: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
}

# 只读完整性校验（verify / require 复验已落盘的快照）
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
  mkdir -p "$dest"

  # ① 数据库走在线备份 API 生成一致快照（合并 WAL，不留 -wal/-shm）
  echo "🩺 SQLite 一致性快照 + quick_check"
  local db base
  shopt -s nullglob
  for db in "$DATA_DIR"/*.db; do
    base="$(basename "$db")"
    if snapshot_db "$db" "${dest}/${base}"; then
      ok "snapshot + quick_check ${base}"
    else
      bad "快照或校验失败: ${base}（备份不可信，已删除）"
      rm -rf "$dest"
      exit 1
    fi
  done

  # ② 其余文件（json / lock / 子目录如 qdrant）原样拷贝。
  #    -wal/-shm 刻意排除：内容已被快照合并，留着只会让校验和永远漂移。
  local item name
  for item in "$DATA_DIR"/* "$DATA_DIR"/.[!.]*; do
    [[ -e "$item" ]] || continue
    name="$(basename "$item")"
    case "$name" in
      *.db|*.db-wal|*.db-shm|*.db-journal) continue ;;
    esac
    cp -a "$item" "${dest}/${name}"
  done
  shopt -u nullglob

  # ③ sha256 放在最后算：原实现是「先算校验和再 quick_check」，
  #    而 quick_check 打开库会重建 -shm 并 checkpoint WAL，
  #    当场把刚算好的基线打废（实机：create 通过、require 立刻拒绝）。
  echo "🔐 生成 sha256 校验和"
  ( cd "$dest" && find . -type f ! -name SHA256SUMS ! -name .backup_verified \
      -exec sh -c 'sha256sum "$1" 2>/dev/null || shasum -a 256 "$1"' _ {} \; > SHA256SUMS )

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

#!/usr/bin/env bash
# Local per-file snapshots; cross-store consistency and live server snapshots
# require a separately coordinated operations procedure.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${AIDUMEM_HOME:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
DATA_DIR="${AIDUMEM_DATA_DIR:-${REPO_ROOT}/data}"
BACKUP_ROOT="${AIDUMEM_BACKUP_ROOT:-${REPO_ROOT}/backups}"
PY="${AIDUMEM_PYTHON:-python3}"
source "${SCRIPT_DIR}/_backup_common.sh"

case "${1:-}" in
  create)
    out="$("$PY" "${SCRIPT_DIR}/restore_bundle.py" create "$DATA_DIR" "$BACKUP_ROOT" "${2:-migration}")"
    printf '%s\n' "$out"
    dest="$(printf '%s\n' "$out" | tail -n 1)"
    # f0.8+ ④：模型/嵌入配置（含密钥）不在 data/ 里，打成备份目录旁的 <备份>.config.tgz（0600）。
    # 放在目录外：备份目录的成员清单保持严格不变，恢复也不会把密钥带进 data/。
    cfg="${AIDUMEM_CONFIG_FILE:-${REPO_ROOT}/mem0_config_local.json}"
    if [[ -f "$cfg" && -d "$dest" ]]; then
      tar -czf "${dest}.config.tgz" -C "$(dirname "$cfg")" "$(basename "$cfg")"
      chmod 600 "${dest}.config.tgz"
      echo "config: ${dest}.config.tgz"
    fi
    ;;
  verify)
    dest="$(resolve_latest "${2:?backup directory required}" "$BACKUP_ROOT")" || {
      echo 'FAIL: latest backup directory not found' >&2; exit 1;
    }
    exec "$PY" "${SCRIPT_DIR}/restore_bundle.py" verify "$dest"
    ;;
  require)
    exec "$PY" "${SCRIPT_DIR}/restore_bundle.py" require "$BACKUP_ROOT"
    ;;
  *) echo "usage: $0 {create <label>|verify <dir|latest>|require}" >&2; exit 2 ;;
esac

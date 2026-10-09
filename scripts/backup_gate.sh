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
    exec "$PY" "${SCRIPT_DIR}/restore_bundle.py" create "$DATA_DIR" "$BACKUP_ROOT" "${2:-migration}"
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

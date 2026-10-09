#!/usr/bin/env bash
# Verify backups, or restore files ONLY into a fresh isolated target.
# No service is contacted or started. An exit-zero restore is not a service drill.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${AIDUMEM_HOME:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
DATA_DIR="${AIDUMEM_DATA_DIR:-${ROOT}/data}"
BACKUP_ROOT="${AIDUMEM_BACKUP_ROOT:-${ROOT}/backups}"
PY="${AIDUMEM_PYTHON:-python3}"
source "${SCRIPT_DIR}/_backup_common.sh"

# A managed local SQLite readback is deliberately separate from file restore.
# Fixture JSON identifies an EXISTING historical row and its expected value hash.
if [[ "${1:-}" == "--drill" ]]; then
  [[ $# -eq 4 ]] || { echo "usage: $0 --drill <target> <snapshot_id> <fixture.json>" >&2; exit 2; }
  exec "$PY" "${SCRIPT_DIR}/restore_bundle.py" drill "$2" "$3" "$4"
fi
MODE=restore
if [[ "${1:-}" == "--dry-run" ]]; then MODE=verify; shift; fi
if [[ "${1:-}" == "--isolated" ]]; then shift; fi
[[ $# -eq 1 ]] || { echo "usage: $0 [--dry-run|--isolated] <backup_dir|latest>" >&2; exit 2; }
BACKUP_DIR="$(resolve_latest "$1" "$BACKUP_ROOT")" || { echo 'FAIL: backup directory not found' >&2; exit 3; }
if [[ "$MODE" == verify ]]; then
  exec "$PY" "${SCRIPT_DIR}/restore_bundle.py" verify "$BACKUP_DIR"
fi
if [[ "${RESTORE_GATE_ALLOW_APPLY:-0}" != 1 ]]; then
  echo 'FAIL: apply disabled. Set RESTORE_GATE_ALLOW_APPLY=1 and AIDUMEM_DATA_DIR to a nonexistent isolated target; no live overlay or service proof.' >&2
  exit 4
fi
exec "$PY" "${SCRIPT_DIR}/restore_bundle.py" restore "$BACKUP_DIR" "$DATA_DIR"

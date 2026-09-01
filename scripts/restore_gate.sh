#!/bin/bash
# Restore gate: verify a backup before and prove the service after restoration.
set -euo pipefail

usage() {
  echo "usage: $0 [--dry-run] <backup_dir>" >&2
}

MODE="apply"
if [[ "${1:-}" == "--dry-run" ]]; then
  MODE="dry-run"
  shift
fi
if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

BACKUP_DIR="$1"
ROOT="${AIDUMEM_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${AIDUMEM_DATA_DIR:-${ROOT}/data}"
API_BASE="${AIDUMEM_API_BASE:-http://127.0.0.1:8767}"
BACKUP_ROOT="${AIDUMEM_BACKUP_ROOT:-${ROOT}/backups}"
export AIDUMEM_DATA_DIR="${DATA_DIR}"

# 共享 helper（v20.3.1 · 外审 Qwen P1-7）：latest 解析与 backup_gate 同一份实现。
# cron 的 `restore_gate.sh --dry-run latest` 此前把 latest 当字面目录名 → 每周必失败。
# shellcheck source=scripts/_backup_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_backup_common.sh"
if ! BACKUP_DIR="$(resolve_latest "${BACKUP_DIR}" "${BACKUP_ROOT}")"; then
  echo "FAIL: no verified backup found under ${BACKUP_ROOT} (tried to resolve 'latest')" >&2
  exit 3
fi

if [[ ! -d "${BACKUP_DIR}" ]]; then
  echo "FAIL: backup directory not found: ${BACKUP_DIR}" >&2
  exit 3
fi

SHA_FILE="${BACKUP_DIR}/SHA256SUMS"
VERIFY_FILE="${BACKUP_DIR}/.backup_verified"
if [[ ! -f "${SHA_FILE}" ]]; then
  echo "FAIL: backup has no SHA256SUMS: ${BACKUP_DIR}" >&2
  exit 3
fi
if ! (cd "${BACKUP_DIR}" && sha256sum -c "${SHA_FILE}" >/tmp/aidumei_restore_sha.log 2>&1); then
  echo "FAIL: sha256 verification failed" >&2
  exit 3
fi
if [[ ! -f "${VERIFY_FILE}" ]]; then
  echo "FAIL: backup lacks .backup_verified marker" >&2
  exit 3
fi

for db in "${BACKUP_DIR}"/*.db; do
  [[ -e "$db" ]] || continue
  if ! python3 - "$db" <<'PY' >/tmp/aidumei_restore_quick.log 2>&1
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    result = conn.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise SystemExit(f"quick_check failed: {result}")
finally:
    conn.close()
PY
  then
    echo "FAIL: quick_check failed: ${db}" >&2
    exit 3
  fi
done

if [[ "${MODE}" == "dry-run" ]]; then
  echo "PASS: backup verification dry-run ok"
  echo "backup_dir=$(basename "${BACKUP_DIR}")"
  echo "db_count=$(find "${BACKUP_DIR}" -maxdepth 1 -name '*.db' | wc -l | tr -d ' ')"
  exit 0
fi

if [[ "${RESTORE_GATE_ALLOW_APPLY:-0}" != "1" ]]; then
  cat >&2 <<'MSG'
FAIL: apply mode is disabled by default. Restoring over live data is destructive.
Set RESTORE_GATE_ALLOW_APPLY=1 and a writable test AIDUMEM_DATA_DIR to run this gate.
MSG
  exit 4
fi

mkdir -p "${DATA_DIR}"
for db in "${BACKUP_DIR}"/*.db; do
  [[ -e "$db" ]] || continue
  name=$(basename "$db")
  cp "${db}" "${DATA_DIR}/${name}"
done

if ! python3 - "$API_BASE" <<'PYCURL' >/dev/null
import sys, urllib.request
from ducky.utils import api_auth_headers
headers = api_auth_headers()  # Sets Authorization when configured.
req = urllib.request.Request(sys.argv[1] + "/health", headers=headers)
urllib.request.urlopen(req, timeout=10).read()
PYCURL
then
  echo "FAIL: service health check failed after restore" >&2
  exit 5
fi
if ! python3 "${ROOT}/scripts/e2e_smoke.py" --json >/tmp/aidumei_restore_e2e.json; then
  echo "FAIL: e2e smoke failed after restore" >&2
  exit 5
fi

echo "PASS: restore gate completed"
echo "backup_dir=$(basename "${BACKUP_DIR}")"
echo "db_count=$(find "${DATA_DIR}" -maxdepth 1 -name '*.db' | wc -l | tr -d ' ')"

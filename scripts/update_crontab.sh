#!/bin/bash
# Install or inspect aiduMEI periodic maintenance jobs.
set -euo pipefail

REPO_ROOT="${AIDUMEM_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [[ -n "${AIDUMEM_PYTHON:-}" ]]; then
  PY="${AIDUMEM_PYTHON}"
elif [[ -x "${REPO_ROOT}/venv/bin/python3" ]]; then
  PY="${REPO_ROOT}/venv/bin/python3"
elif [[ -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
  PY="${REPO_ROOT}/.venv/bin/python3"
else
  PY="python3"
fi
LOG_DIR="${REPO_ROOT}/logs"
BACKUP_ROOT="${AIDUMEM_BACKUP_ROOT:-${REPO_ROOT}/backups}"
mkdir -p "${LOG_DIR}" "${BACKUP_ROOT}"

MODE="${1:-install}"
if [[ "${MODE}" != "--list" && "${MODE}" != "--dry-run" && "${MODE}" != "install" ]]; then
  echo "usage: $0 [--list|--dry-run|install]" >&2
  exit 2
fi

# Each row: name|schedule|command|log|owner|failure_action
# Only commands backed by scripts in this repository are eligible.
TASKS=(
  "health_check|*/5 * * * *|\"${PY}\" scripts/health_check.py|health_check.log|system|check journal and run report.py"
  "consolidator|0 4 * * *|\"${PY}\" scripts/consolidator.py|consolidator.log|memory|run report.py and inspect stale facts"
  "backup_create|30 2 * * *|bash scripts/backup_gate.sh create daily|backup_create.log|data|restore last verified backup"
  "backup_verify|0 3 * * 0|bash scripts/backup_gate.sh verify latest|backup_verify.log|data|run backup_gate.sh create and verify"
  "e2e_smoke|15 * * * *|\"${PY}\" scripts/e2e_smoke.py --json|e2e_smoke.log|quality|inspect trace and rerun tenant-scoped smoke"
  "facts_checkpoint|0 4 * * 0|\"${PY}\" scripts/facts_maintenance.py checkpoint|facts_checkpoint.log|database|run backup and restore_gate dry-run"
  "report|0 * * * *|\"${PY}\" scripts/report.py --json|report.log|operations|inspect report next_actions"
  "restore_gate_dry_run|30 3 * * 0|bash scripts/restore_gate.sh --dry-run latest|restore_gate.log|data|stop changes and run restore_gate"
  "dependency_audit|45 3 * * 0|\"${PY}\" scripts/dependency_audit.py|dependency_audit.log|platform|pin or update dependencies deliberately"
)

json_tasks() {
  python3 - "${TASKS[@]}" <<'PY'
import json, sys
tasks = []
for row in sys.argv[1:]:
    name, schedule, command, log, owner, failure = row.split("|", 5)
    tasks.append({"name": name, "schedule": schedule, "command": command,
                  "log": log, "owner": owner, "failure_action": failure})
print(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2))
PY
}

if [[ "${MODE}" == "--list" ]]; then
  json_tasks
  exit 0
fi

entries=()
for row in "${TASKS[@]}"; do
  IFS='|' read -r name schedule command log owner failure <<< "$row"
  # Refuse to advertise an executable that is not present.
  first="${command%% *}"
  if [[ "${command}" == "\"${PY}\" "* ]]; then
    first="${PY}"
  elif [[ "${first}" == "bash" ]]; then
    first="${command#bash }"
    first="${first%% *}"
  elif [[ "${first}" == "\""* ]]; then
    first="${command#\"}"
    first="${first%%\" *}"
  fi
  if [[ "${first}" == "${PY}" ]]; then
    if [[ ! -x "${first}" ]]; then
      echo "missing executable: ${first}" >&2
      exit 1
    fi
  else
    if [[ ! -x "${REPO_ROOT}/${first}" ]]; then
      echo "missing executable: ${REPO_ROOT}/${first}" >&2
      exit 1
    fi
  fi
  entries+=("# aiduMEI: 每日记忆巩固 | ${name}|owner=${owner}|failure=${failure}
${schedule} cd \"${REPO_ROOT}\" && ${command} >> \"${LOG_DIR}/${log}\" 2>&1")
done

if [[ "${MODE}" == "--dry-run" ]]; then
  printf '%s\n' "DRY-RUN: would install ${#entries[@]} aiduMEI maintenance tasks."
  printf '%s\n' "${entries[@]}"
  exit 0
fi

BACKUP_FILE="${LOG_DIR}/crontab_backup_$(date +%Y%m%d_%H%M%S).txt"
crontab -l > "${BACKUP_FILE}" 2>/dev/null || true
TMP=$(mktemp)
crontab -l 2>/dev/null > "${TMP}" || true
for entry in "${entries[@]}"; do
  name=$(sed -n 's/^# aiduMEI:\([^|]*\).*/\1/p' <<< "$entry")
  if ! grep -q "aiduMEI:${name}|" "${TMP}"; then
    printf '\n%s\n' "$entry" >> "${TMP}"
  fi
done
crontab "${TMP}"
rm -f "${TMP}"
echo "Installed/verified aiduMEI maintenance tasks: ${#entries[@]}"
echo "Crontab backup: ${BACKUP_FILE}"

#!/bin/bash
# Install or inspect aiduMEI periodic maintenance jobs.
#
# v20.3.1（九份审计整改 P0-1）修掉三个真缺陷：
#   1. 去重键取的是九条任务共享的中文前缀，install 只写进 1 条却报「9」；
#      现在注释头把任务名放在最前，去重键逐任务唯一。
#   2. facts_checkpoint 指向一个仓库里从不存在的脚本；删除（checkpoint 能力
#      已由 backup/restore 链覆盖，不留第二个入口）。任务数 9 → 8。
#   3. `[[ -x python3 ]]` 测的是 ./python3 不是 PATH；PY 现在一律解析成绝对
#      路径，可执行判据才有意义。
#   另：install 完成后回读**真实 crontab** 对账，世界没装够就红；--installed
#   报告实装数（report.py 用它，不再拿意图数冒充实装数）。
set -euo pipefail

REPO_ROOT="${AIDUMEM_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [[ -n "${AIDUMEM_PYTHON:-}" ]]; then
  PY="${AIDUMEM_PYTHON}"
elif [[ -x "${REPO_ROOT}/venv/bin/python3" ]]; then
  PY="${REPO_ROOT}/venv/bin/python3"
elif [[ -x "${REPO_ROOT}/.venv/bin/python3" ]]; then
  PY="${REPO_ROOT}/.venv/bin/python3"
else
  # PATH 兜底也解析成绝对路径：cron 环境 PATH 窄，裸名 python3 到点会找不到。
  PY="$(command -v python3 || true)"
  if [[ -z "${PY}" ]]; then
    echo "python3 not found on PATH; set AIDUMEM_PYTHON explicitly" >&2
    exit 1
  fi
fi
LOG_DIR="${REPO_ROOT}/logs"
BACKUP_ROOT="${AIDUMEM_BACKUP_ROOT:-${REPO_ROOT}/backups}"

usage() {
  echo "usage: $0 [--list|--installed|--dry-run|install]" >&2
}

MODE="${1:-install}"
case "${MODE}" in
  --list|--installed|--dry-run|install) ;;
  *) usage; exit 2 ;;
esac

# 建目录只发生在 install（真正要写 crontab 的动作）。
# --list/--installed 全程只读；--dry-run 也不建——九份审计整改：dry-run 的职责
# 是「把要装的东西给你看」，建目录是 install 的副作用；守卫红了还留下半建的
# 目录，是拿探针当生产用（v20.2.5「-i 环境下 mkdir //backups 炸掉 dry-run」实测）。
if [[ "${MODE}" == "install" ]]; then
  mkdir -p "${LOG_DIR}" "${BACKUP_ROOT}"
fi

# Each row: name|schedule|command|log|owner|failure_action
# Only commands backed by scripts in this repository are eligible.
TASKS=(
  "health_check|*/5 * * * *|\"${PY}\" scripts/health_check.py|health_check.log|system|check journal and run report.py"
  "consolidator|0 4 * * *|\"${PY}\" scripts/consolidator.py|consolidator.log|memory|run report.py and inspect stale facts"
  "backup_create|30 2 * * *|bash scripts/backup_gate.sh create daily|backup_create.log|data|restore last verified backup"
  "backup_verify|0 3 * * 0|bash scripts/backup_gate.sh verify latest|backup_verify.log|data|run backup_gate.sh create and verify"
  "e2e_smoke|15 * * * *|\"${PY}\" scripts/e2e_smoke.py --json|e2e_smoke.log|quality|inspect trace and rerun tenant-scoped smoke"
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

if [[ "${MODE}" == "--installed" ]]; then
  # 数真实 crontab 里本仓拥有的条目，不是数我们打算装什么。
  installed="$(crontab -l 2>/dev/null | grep -c '^# aiduMEI:' || true)"
  printf '{"installed": %s, "expected": %s}\n' "${installed:-0}" "${#TASKS[@]}"
  exit 0
fi

entries=()
for row in "${TASKS[@]}"; do
  IFS='|' read -r name schedule command log owner failure <<< "$row"
  # 守卫：拒绝宣告任何背后没有真实文件的任务。
  if [[ "${command}" == "\"${PY}\" "* ]]; then
    [[ -x "${PY}" ]] || { echo "missing executable: ${PY}" >&2; exit 1; }
    target="${command#*\"${PY}\" }"; target="${target%% *}"
    [[ -f "${REPO_ROOT}/${target}" ]] || { echo "missing target script: ${REPO_ROOT}/${target}" >&2; exit 1; }
  elif [[ "${command}" == bash\ * ]]; then
    target="${command#bash }"; target="${target%% *}"
    [[ -f "${REPO_ROOT}/${target}" ]] || { echo "missing script: ${REPO_ROOT}/${target}" >&2; exit 1; }
  else
    first="${command%% *}"; first="${first#\"}"; first="${first%%\"*}"
    [[ -x "${REPO_ROOT}/${first}" ]] || { echo "missing executable: ${REPO_ROOT}/${first}" >&2; exit 1; }
  fi
  # 注释头把任务名放最前，去重键逐任务唯一。
  entries+=("# aiduMEI:${name}|owner=${owner}|failure=${failure}
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
added=0
for entry in "${entries[@]}"; do
  name="$(sed -n 's/^# aiduMEI:\([^|]*\)|.*/\1/p' <<< "$entry")"
  # 同时认新格式（aiduMEI:NAME|）与旧格式（aiduMEI: … | NAME|），重跑不重复。
  if ! grep -qE "^# aiduMEI:.*${name}\|" "${TMP}"; then
    printf '\n%s\n' "$entry" >> "${TMP}"
    added=$((added + 1))
  fi
done
crontab "${TMP}"
rm -f "${TMP}"

# 与世界对账，而不是与自己数组里的数字对账。
installed="$(crontab -l 2>/dev/null | grep -c '^# aiduMEI:' || true)"
installed="${installed:-0}"
expected="${#entries[@]}"
echo "Installed aiduMEI maintenance tasks: ${installed}/${expected} (added ${added} this run)"
echo "Crontab backup: ${BACKUP_FILE}"
if [[ "${installed}" -ne "${expected}" ]]; then
  echo "FAIL: crontab holds ${installed} aiduMEI entries, expected ${expected}" >&2
  exit 1
fi

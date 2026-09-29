#!/bin/bash
# Shared upgrade checks. Sourced by pre/post scripts after REPO_ROOT and
# VENV_PY are selected; functions are also safe to exercise in isolation.

upgrade_python_for_repo() {
  local repo="$1"
  local candidate
  if [[ -n "${AIDUMEM_PYTHON:-}" ]]; then
    candidate="$(command -v "${AIDUMEM_PYTHON}" || true)"
    [[ -n "${candidate}" && -x "${candidate}" ]] || return 1
    printf '%s\n' "${candidate}"
    return 0
  fi
  for candidate in \
    "${repo}/venv/bin/python3" "${repo}/venv/bin/python" \
    "${repo}/.venv/bin/python3" "${repo}/.venv/bin/python"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  candidate="$(command -v python3 || true)"
  [[ -n "${candidate}" && -x "${candidate}" ]] || return 1
  printf '%s\n' "${candidate}"
}

upgrade_now_ms() {
  "${VENV_PY}" -c 'import time; print(time.monotonic_ns() // 1_000_000)'
}

run_upgrade_smoke() {
  local smoke_script="${REPO_ROOT}/scripts/e2e_smoke.py"
  local output
  if [[ ! -f "${smoke_script}" ]]; then
    echo "missing required E2E smoke script: ${smoke_script}" >&2
    return 1
  fi
  if ! output="$("${VENV_PY}" "${smoke_script}" --json 2>&1)"; then
    printf '%s\n' "${output}" >&2
    return 1
  fi
  printf '%s\n' "${output}"
  # A wrapper that exits 0 while returning WARN must never open the gate.
  printf '%s' "${output}" | "${VENV_PY}" -c '
import json, sys
try:
    report = json.load(sys.stdin)
    good = (report.get("status") == "PASS"
            and report.get("failures") == 0
            and report.get("warnings") == 0)
except (ValueError, AttributeError):
    good = False
sys.exit(0 if good else 1)
'
}

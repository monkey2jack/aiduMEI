#!/bin/bash
# Non-destructive autoshift drill for the running aiduMEI instance.
# Modes:
#   --check  verify script contract without contacting the service
#   --run    verify live health fields and emit JSON (default)
#
# v20.3.1（九份审计 P0-4 · 嘟嘟 🔴-2）：--run 从写出来那天起没有成功跑通过一次：
#   1. heredoc 与管道抢同一个 stdin —— python 从 heredoc 读源码，
#      `sys.stdin.read()` 拿到空串 → JSONDecodeError；
#   2. 断言的 engine_mode / llm_gear / pending_replay 三个键在 /health
#      **顶层不存在**（真身在 probes.* 下）→ 就算修好 stdin 也是全 FAIL；
#   3. `AUTH_ARGS=()` 空数组在 bash 3.2 + set -u 下 unbound variable；
#   4. token 上命令行（ps 可见）。
set -euo pipefail

ROOT="${AIDUMEM_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
API_BASE="${AIDUMEM_API_BASE:-http://127.0.0.1:8767}"
MODE="${1:---run}"
OUT="${OUT:-/tmp/aidumei_autoshift_drill.json}"
TOKEN="${AIDUMEM_API_TOKEN:-}"

if [[ "${MODE}" == "--check" ]]; then
  test -x "${ROOT}/scripts/e2e_smoke.py"
  test -x "${ROOT}/scripts/report.py"
  test -x "${ROOT}/scripts/drill_autoshift.sh"
  # v20.3.1（嘟嘟 🟢-2）：--check 顺手静态断言 --run 要读的键在 health.py 里
  # 存在 —— 否则 --check 绿永远掩盖 --run 死。
  grep -q "engine_mode_policy" "${ROOT}/ducky/hot/health.py"
  grep -q "llm_gear" "${ROOT}/ducky/hot/health.py"
  grep -q "pending_embeddings" "${ROOT}/ducky/hot/health.py"
  echo "PASS: autoshift drill contract present"
  exit 0
elif [[ "${MODE}" != "--run" ]]; then
  echo "usage: $0 [--check|--run]" >&2
  exit 2
fi

if ! curl -fsS "${API_BASE}/health" >/dev/null; then
  echo "FAIL: service is not reachable at ${API_BASE}" >&2
  exit 3
fi

# v20.3.1：token 不上命令行（curl 的 -H 参数在 ps 里可见），
# 改经 stdin 传头定义。
HEALTH_FILE="$(mktemp)"
trap 'rm -f "${HEALTH_FILE}"' EXIT
if [[ -n "${TOKEN}" ]]; then
  # shellcheck disable=SC2016
  curl -fsS -H @- "${API_BASE}/health" <<< "Authorization: Bearer ${TOKEN}" > "${HEALTH_FILE}"
else
  curl -fsS "${API_BASE}/health" > "${HEALTH_FILE}"
fi

# v20.3.1：heredoc 与管道抢 stdin 的旧病 —— HEALTH 先落临时文件，
# python 脚本经 -c 传入，读文件而不是读 stdin。
python3 -c '
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    health = json.load(f)
probes = health.get("probes") or {}
engine_policy = probes.get("engine_mode_policy") or {}
llm_gear = probes.get("llm_gear") or {}
pending = probes.get("pending_embeddings") or {}
checks = {
    "engine_mode_present": isinstance(engine_policy.get("configured") or engine_policy.get("mode"), str),
    "llm_gear_present": isinstance(llm_gear, dict),
    "pending_replay_present": "pending_count" in pending or isinstance(pending, dict),
    "warming_up_present": isinstance(health.get("warming_up"), list),
    "degraded_present": isinstance(health.get("degraded"), list),
}
result = {
    "status": "pass" if all(checks.values()) else "fail",
    "checks": checks,
    "engine_mode": engine_policy.get("configured") or engine_policy.get("mode"),
    "llm_gear": llm_gear,
    "pending_replay": pending,
    "warming_up": health.get("warming_up"),
    "degraded": health.get("degraded"),
}
with open(sys.argv[2], "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2))
if result["status"] != "pass":
    raise SystemExit(3)
' "${HEALTH_FILE}" "${OUT}"

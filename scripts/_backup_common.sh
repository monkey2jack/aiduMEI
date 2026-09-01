#!/bin/bash
# Shared backup helpers (v20.3.1 · 外审 Qwen P1-7 整改).
#
# 单一真相源：backup_gate.sh 与 restore_gate.sh 共用的 latest 解析。
# 此前两边各认显式目录、互不解析 latest —— cron 任务传 `verify latest`
# 每周必失败。契约抄两遍必漏一遍（本仓 v20.1 已记过账），所以收在这里。

# resolve_latest <arg> <backup_root>
#   arg == "latest" → 在 backup_root 下找 mtime 最新的含 SHA256SUMS 的备份目录
#   其余值原样返回（显式路径形态保持不变）
resolve_latest() {
  local arg="$1" root="$2"
  if [[ "${arg}" != "latest" ]]; then
    printf '%s' "${arg}"
    return 0
  fi
  if [[ ! -d "${root}" ]]; then
    return 1
  fi
  local latest="" d
  for d in "${root}"/*/; do
    [[ -d "$d" ]] || continue
    [[ -f "${d}SHA256SUMS" ]] || continue
    if [[ -z "${latest}" ]] || [[ "${d}" -nt "${latest}" ]]; then
      latest="${d%/}"
    fi
  done
  if [[ -z "${latest}" ]]; then
    return 1
  fi
  printf '%s' "${latest}"
}

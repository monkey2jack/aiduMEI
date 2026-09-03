#!/usr/bin/env bash
# 推送前闸门 —— 四道关不过就**不推**。
# 立此脚本的原因：我把 `git push` 无条件串在闸门后面跑，闸门报了「硬敏感命中 1 次」
# 我没读退出码，带着命中把提交推进了小仓。铁律 0 写着「任一面命中 → 立即停推」，
# 而我用一条 `&&` 把那句话作废了。纪律靠记性执行，早晚会失效一次 —— 焊成脚本。
set -e
cd "$(git rev-parse --show-toplevel)"
unset ALL_PROXY all_proxy http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
# v20.3.2 正式版（用户审计 C）：解释器不许写死 `.venv/`。生产是 `venv/`（无点），
# 这条闸门在生产从来跑不起来 —— 而 acceptance_check.sh 里早有正确的探测链，
# 本文件没用上（「惯用法存在不等于被一致套用」）。现在同一条链。
PY="${AIDUMEM_PYTHON:-}"
if [[ -z "${PY}" ]]; then
  for cand in ./.venv/bin/python ./venv/bin/python; do
    if [[ -x "${cand}" ]]; then PY="${cand}"; break; fi
  done
fi
if [[ -z "${PY}" ]]; then
  for cand in python3 python3.12 python3.11 python3.10; do
    if command -v "${cand}" >/dev/null 2>&1 && "${cand}" -c "import pytest" >/dev/null 2>&1; then
      PY="${cand}"; break
    fi
  done
fi
[[ -n "${PY}" ]] || { echo "🛑 [停推] 找不到带 pytest 的解释器（设 AIDUMEM_PYTHON）"; exit 1; }
fail() { echo "🛑 [停推] $1"; exit 1; }

"$PY" -m pytest tests/ -q > /tmp/g_t.log 2>&1 || fail "测试关未过：$(tail -1 /tmp/g_t.log)"

# v20.2.5：静态关。只拦**真缺陷类** —— F821 未定义名（运行时 NameError，
# 本版就抓到一条被 except 吞了很久的）、F811 重复定义。F841「算了不用」走
# 登记制（tests 里的基线守卫），不在这里阻塞：存量里混着无害残留，
# 一次全拦会让人绕过整道关。
if "$PY" -c "import ruff" >/dev/null 2>&1; then
  "$PY" -m ruff check ducky/ api_server.py mcp_server.py scripts/ conftest.py tests/ \
      --select F821,F811 --output-format concise > /tmp/g_ruff.log 2>&1 \
      || fail "静态关未过（F821/F811 是运行时会炸的形态）：$(head -3 /tmp/g_ruff.log | tr '\n' ' ')"
  echo "  ✅ 静态关：F821/F811 零命中"
else
  # 生产 venv 不装 lint 工具。**显式 SKIP 并计入**，不许静默当过（那就是假绿灯），
  # 也不许假红（那会逼人绕过整道关）。开发机推送前必须装 ruff。
  echo "  ⚪ 静态关：SKIP（本解释器无 ruff；此关由开发机推送前承担）"
  GATE_SKIPPED="${GATE_SKIPPED:-}static "
fi
echo "  ✅ 测试关：$(tail -1 /tmp/g_t.log)"

"$PY" -m compileall -q ducky api_server.py mcp_server.py mem0_sync.py tests scripts benchmarks > /tmp/g_c.log 2>&1 \
  || fail "编译关未过"
echo "  ✅ 编译关：0 语法错误"

# bash 3.2（macOS 自带 /bin/bash）没有 mapfile/readarray 内建 —— 用 while read 装数组，两边都跑得通。
files=()
while IFS= read -r _f; do [[ -n "${_f}" ]] && files+=("${_f}"); done < <({ git ls-files; git ls-files --others --exclude-standard; } | sort -u)
txt=(); for f in "${files[@]}"; do
  case "$f" in *.png|*.jpg|*.jpeg|*.gif|*.ico|*.webp|*.woff|*.woff2|*.ttf|*.gz|*.zip|*.pyc);;
  *) [[ -f "$f" ]] && txt+=("$f");; esac
done
AIDUMEI_SCAN_WORDLIST="$HOME/.config/aidumei/scan_words.txt" \
  "$PY" scripts/release_scan.py "${txt[@]}" > /tmp/g_s.log 2>&1 \
  || fail "脱密关·面①未过：$(grep '总计硬敏感命中' /tmp/g_s.log)"
echo "  ✅ 脱密关面①：$(grep '总计硬敏感命中' /tmp/g_s.log)（射程 ${#txt[@]}）"

git log --format='%B' upstream/main..HEAD > /tmp/g_m.txt 2>/dev/null || true
if [ -s /tmp/g_m.txt ]; then
  AIDUMEI_SCAN_WORDLIST="$HOME/.config/aidumei/scan_words.txt" \
    "$PY" scripts/release_scan.py /tmp/g_m.txt > /tmp/g_m.log 2>&1 \
    || fail "脱密关·面②（提交信息）未过"
  echo "  ✅ 脱密关面②：$(grep '总计硬敏感命中' /tmp/g_m.log)"
fi
if [[ -n "${GATE_SKIPPED:-}" ]]; then
  echo "  ── 四道关：$(echo "${GATE_SKIPPED}" | wc -w | tr -d ' ') 关 SKIP（${GATE_SKIPPED}），其余全过 ──"
else
  echo "  ── 四道关全过，可以推 ──"
fi

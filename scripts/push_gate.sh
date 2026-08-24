#!/bin/zsh
# 推送前闸门 —— 三道关不过就**不推**。
# 立此脚本的原因：我把 `git push` 无条件串在闸门后面跑，闸门报了「硬敏感命中 1 次」
# 我没读退出码，带着命中把提交推进了小仓。铁律 0 写着「任一面命中 → 立即停推」，
# 而我用一条 `&&` 把那句话作废了。纪律靠记性执行，早晚会失效一次 —— 焊成脚本。
set -e
cd "$(git rev-parse --show-toplevel)"
unset ALL_PROXY all_proxy http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
PY=./.venv/bin/python
fail() { echo "🛑 [停推] $1"; exit 1; }

$PY -m pytest tests/ -q > /tmp/g_t.log 2>&1 || fail "测试关未过：$(tail -1 /tmp/g_t.log)"
echo "  ✅ 测试关：$(tail -1 /tmp/g_t.log)"

$PY -m compileall -q ducky api_server.py mcp_server.py mem0_sync.py tests scripts benchmarks > /tmp/g_c.log 2>&1 \
  || fail "编译关未过"
echo "  ✅ 编译关：0 语法错误"

files=("${(@f)$(git ls-files; git ls-files --others --exclude-standard)}")
txt=(); for f in $files; do
  case "$f" in *.png|*.jpg|*.jpeg|*.gif|*.ico|*.webp|*.woff|*.woff2|*.ttf|*.gz|*.zip|*.pyc);;
  *) [[ -f "$f" ]] && txt+=("$f");; esac
done
AIDUMEI_SCAN_WORDLIST="$HOME/.config/aidumei/scan_words.txt" \
  $PY scripts/release_scan.py "${txt[@]}" > /tmp/g_s.log 2>&1 \
  || fail "脱密关·面①未过：$(grep '总计硬敏感命中' /tmp/g_s.log)"
echo "  ✅ 脱密关面①：$(grep '总计硬敏感命中' /tmp/g_s.log)（射程 ${#txt[@]}）"

git log --format='%B' upstream/main..HEAD > /tmp/g_m.txt 2>/dev/null || true
if [ -s /tmp/g_m.txt ]; then
  AIDUMEI_SCAN_WORDLIST="$HOME/.config/aidumei/scan_words.txt" \
    $PY scripts/release_scan.py /tmp/g_m.txt > /tmp/g_m.log 2>&1 \
    || fail "脱密关·面②（提交信息）未过"
  echo "  ✅ 脱密关面②：$(grep '总计硬敏感命中' /tmp/g_m.log)"
fi
echo "  ── 三道关全过，可以推 ──"

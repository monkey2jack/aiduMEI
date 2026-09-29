#!/usr/bin/env bash
# scripts/install-hooks.sh — 把 push_gate.sh 装成真正的 git pre-push 钩子
#
# v22.0（雷霆审计 B9 · Sonnet P0-3）：push_gate.sh 此前只在 CONTRIBUTING.md
# 里被「记得跑」——纪律靠记性执行，早晚失效一次。本脚本让「忘记跑」从
# 物理上不可能发生。
#
# f0.3（身份闸补漏）：旧钩子只在 push 到 refs/heads/main|master 时才跑
# push_gate.sh，于是**标签与其它分支从来不扫**——公开的 f0.2 标签对象
# tagger 就带着内网 IP 邮箱推了出去。现在两道闸：
#   ① 每个被推送的引用（分支与标签，一个不跳）先过 commit_metadata_scan.py
#      --pre-push：新提交扫 author + committer，附注标签扫 tagger；白名单模式
#      （项目身份或 GitHub noreply）。扫描不可核验（缺词表、git 读不出）同样停推。
#   ② 推 main/master 时再跑完整 push_gate.sh（PR merge 由 CI 管，不重复拦）。
#
# 用法：
#   bash scripts/install-hooks.sh          # 安装（幂等；旧版本钩子自动升级）
#   bash scripts/install-hooks.sh --check  # 只查是否已装且为当前版本（退出码）
#   bash scripts/install-hooks.sh --remove # 卸载
#
# 钩子路径取 `git rev-parse --git-path hooks/pre-push`：linked worktree
# （.git 是文件不是目录）与 core.hooksPath 都能装对地方。

set -euo pipefail

TOP=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "❌ 当前目录不在 git 仓库里" >&2
    exit 1
}
cd "$TOP"
HOOK_FILE=$(git rev-parse --git-path hooks/pre-push)
MARKER="# aidumei-pre-push-hook"
VERSION_MARKER="# aidumei-pre-push-hook-version: 2"

has_marker() {
    [ -f "$HOOK_FILE" ] && grep -q "$MARKER" "$HOOK_FILE"
}

is_current() {
    has_marker && grep -qF "$VERSION_MARKER" "$HOOK_FILE"
}

case "${1:-}" in
    --check)
        if is_current; then
            echo "✅ pre-push 钩子已安装（当前版本）"
            exit 0
        elif has_marker; then
            echo "❌ pre-push 钩子是旧版本（只拦 main/master，不扫标签与其它分支）——请重跑安装" >&2
            exit 1
        else
            echo "❌ pre-push 钩子未安装" >&2
            exit 1
        fi
        ;;
    --remove)
        if has_marker; then
            rm -f "$HOOK_FILE"
            echo "✅ 已卸载 pre-push 钩子"
        else
            echo "ℹ️  本就未安装"
        fi
        exit 0
        ;;
    "")
        if is_current; then
            echo "ℹ️  已安装当前版本，幂等跳过"
            exit 0
        fi
        if [ -e "$HOOK_FILE" ] && ! has_marker; then
            echo "❌ $HOOK_FILE 已存在且不是本脚本生成的钩子——拒绝覆盖（请先手动合并或移走）" >&2
            exit 1
        fi
        mkdir -p "$(dirname "$HOOK_FILE")"
        cat > "$HOOK_FILE" <<'EOF'
#!/usr/bin/env bash
# aidumei-pre-push-hook —— push 前的两道闸（f0.3 起分支与标签一律扫描）
# aidumei-pre-push-hook-version: 2
# 由 scripts/install-hooks.sh 生成；要改请改那个脚本。
set -u
remote="${1:-}"
cd "$(git rev-parse --show-toplevel)" || exit 1

PY="${AIDUMEM_PYTHON:-}"
if [ -z "$PY" ]; then
    for cand in ./.venv/bin/python ./venv/bin/python; do
        if [ -x "$cand" ]; then PY="$cand"; break; fi
    done
fi
if [ -z "$PY" ] && command -v python3 >/dev/null 2>&1; then PY=python3; fi
if [ -z "$PY" ]; then
    echo "🛑 pre-push：找不到 Python 解释器（设 AIDUMEM_PYTHON）——push 已取消"
    exit 1
fi

# ① 身份闸：每个被推送的引用都扫（分支与标签），失败或不可核验都停推。
refs=$(cat)
if ! printf '%s\n' "$refs" | \
    AIDUMEI_SCAN_WORDLIST="${AIDUMEI_SCAN_WORDLIST:-$HOME/.config/aidumei/f02_full_scan_words.txt}" \
    "$PY" scripts/commit_metadata_scan.py --pre-push "$remote" --require-allowlist; then
    echo ""
    echo "🛑 身份元数据扫描未过或不可核验——push 已取消（分支与标签一律扫描）"
    echo "   新提交 author/committer 与附注标签 tagger 须为项目身份或 GitHub noreply；"
    echo "   修复后重跑，或确认要跳过请用 --no-verify"
    exit 1
fi

# ② 完整闸：推 main/master 时跑 push_gate.sh（PR merge 由 CI 管，不重复拦）。
run_gate=0
while read -r local_ref local_sha remote_ref remote_sha; do
    case "${remote_ref:-}" in
        refs/heads/main|refs/heads/master) run_gate=1 ;;
    esac
done <<< "$refs"
if [ "$run_gate" = 1 ]; then
    echo "🛡️  push 到 main/master —— 自动运行 push_gate.sh"
    if ! bash scripts/push_gate.sh; then
        echo ""
        echo "🛑 push_gate.sh 未过——push 已取消"
        echo "   修复后重跑，或确认要跳过请用 --no-verify"
        exit 1
    fi
fi
exit 0
EOF
        chmod +x "$HOOK_FILE"
        echo "✅ pre-push 钩子已安装到 $HOOK_FILE"
        echo "   每次 push（分支与标签）先扫身份元数据；push 到 main/master 另跑 push_gate.sh"
        ;;
    *)
        echo "用法: $0 [--check|--remove]" >&2
        exit 1
        ;;
esac

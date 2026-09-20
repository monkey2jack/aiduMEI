#!/usr/bin/env bash
# scripts/install-hooks.sh — 把 push_gate.sh 装成真正的 git pre-push 钩子
#
# v22.0（雷霆审计 B9 · Sonnet P0-3）：push_gate.sh 此前只在 CONTRIBUTING.md
# 里被「记得跑」——纪律靠记性执行，早晚失效一次。本脚本让「忘记跑」从
# 物理上不可能发生。
#
# 用法：
#   bash scripts/install-hooks.sh          # 安装（幂等）
#   bash scripts/install-hooks.sh --check  # 只查是否已装（退出码）
#   bash scripts/install-hooks.sh --remove # 卸载

set -euo pipefail

HOOK_FILE=".git/hooks/pre-push"
MARKER="# aidumei-pre-push-hook"

if [ ! -d ".git" ]; then
    echo "❌ 当前目录不是 git 仓库根" >&2
    exit 1
fi

is_installed() {
    [ -f "$HOOK_FILE" ] && grep -q "$MARKER" "$HOOK_FILE"
}

case "${1:-}" in
    --check)
        if is_installed; then
            echo "✅ pre-push 钩子已安装"
            exit 0
        else
            echo "❌ pre-push 钩子未安装" >&2
            exit 1
        fi
        ;;
    --remove)
        if is_installed; then
            rm -f "$HOOK_FILE"
            echo "✅ 已卸载 pre-push 钩子"
        else
            echo "ℹ️  本就未安装"
        fi
        exit 0
        ;;
    "")
        if is_installed; then
            echo "ℹ️  已安装，幂等跳过"
            exit 0
        fi
        cat > "$HOOK_FILE" <<'EOF'
#!/usr/bin/env bash
# aidumei-pre-push-hook —— v22.0 起，push 前自动跑 push_gate.sh
# 由 scripts/install-hooks.sh 生成；要改请改那个脚本。

# 只拦直接 push 到 main（PR merge 由 CI 管，不重复拦）
while read -r local_ref local_sha remote_ref remote_sha; do
    if [ "$remote_ref" = "refs/heads/main" ] || [ "$remote_ref" = "refs/heads/master" ]; then
        echo "🛡️  push 到 main/master —— 自动运行 push_gate.sh"
        if ! bash scripts/push_gate.sh; then
            echo ""
            echo "🛑 push_gate.sh 未过——push 已取消"
            echo "   修复后重跑，或确认要跳过请用 --no-verify"
            exit 1
        fi
    fi
done
exit 0
EOF
        chmod +x "$HOOK_FILE"
        echo "✅ pre-push 钩子已安装到 $HOOK_FILE"
        echo "   现在直接 push 到 main 会自动跑 push_gate.sh；PR merge 不拦"
        ;;
    *)
        echo "用法: $0 [--check|--remove]" >&2
        exit 1
        ;;
esac

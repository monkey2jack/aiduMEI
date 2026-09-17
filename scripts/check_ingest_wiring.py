#!/usr/bin/env python3
"""check_ingest_wiring — 记忆写入接线自查（部署后第一件该跑的事）

为什么有这个脚本
────────────────
aiduMEI 有两条独立的链路，**必须都接上**才算能用：

    读（注入）：宿主每轮对话前 → 调 /search → 把相关记忆塞进上下文
    写（落库）：宿主每轮对话后 → 调 /add  → 把这轮内容记下来

只接读不接写，系统会表现得**非常正常**：检索有结果、/health 全绿、
指标好看 —— 因为库里那些旧记忆确实健康。但你说的每一句新话都在
看完即丢，你会在几周后才隐约觉得「它怎么什么都不记得」。

2026-09-17 我们在自己的生产部署上踩了这个坑：注入钩子挂了一个月，
写入钩子从没挂过。所有探针全绿，是人工审计翻数据库才发现的。
这个脚本就是那次事故的产物 —— 它只问一个问题：

    **你在读，那你在写吗？**

用法
────
    python3 scripts/check_ingest_wiring.py                  # 查本机默认端口
    python3 scripts/check_ingest_wiring.py --url http://... --token xxx
    python3 scripts/check_ingest_wiring.py --json           # 机器可读

退出码：0 = 接线正常 / 1 = 接线有问题 / 2 = 连不上服务
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 复用仓内既有口径 AIDUMEM_API_BASE（report.py 等同款），不另造新变量名
DEFAULT_URL = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")
DEFAULT_TOKEN = os.getenv("AIDUMEM_API_TOKEN", "")


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _get(url: str, token: str, path: str) -> dict:
    """鉴权头走仓内单一凭据入口 api_auth_headers（env → 仓库根 .env 兜底）。
    `--token` 通过环境变量覆盖，不在这里另起一套取值逻辑 —— 多一套凭据
    读法就多一处会和真实配置漂移的地方。"""
    if token:
        os.environ["AIDUMEM_API_TOKEN"] = token
    try:
        from ducky.utils import api_auth_headers
        headers = api_auth_headers()
    except Exception:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url.rstrip("/") + path)
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def diagnose(health: dict, *, require_judgment: bool = False) -> tuple[int, list[str], dict]:
    """返回 (退出码, 人类可读结论, 关键数字)。判据与 /health 探针同源。"""
    probes = health.get("probes") or {}
    facts = {
        "reads_24h": probes.get("ingest_reads_24h"),
        # 判据用「来自对话的检索」：总数里混着 e2e_smoke 每小时一次的巡检
        # 心跳，实测生产近 24h 的检索记录一条真实对话都没有，拿总数判会在
        # 「今天没聊天」时必然误报写线断了。
        "conv_reads_24h": probes.get("ingest_conv_reads_24h"),
        "writes_24h": probes.get("ingest_writes_24h"),
        # 判据落在这个数上：任何来源的写入里混着 cron 整合器、MEMORY.md
        # 同步引擎等后台通路，它们不为零不代表对话在被记下来。
        "turn_writes_24h": probes.get("ingest_turn_writes_24h"),
        "liveness_ok": probes.get("ingest_liveness_ok"),
        "session_coverage": probes.get("epistemic_session_coverage"),
        "distill_sessions_24h": probes.get("distill_sessions_24h"),
        "distill_made_24h": probes.get("distill_made_24h"),
        "distill_liveness_ok": probes.get("distill_liveness_ok"),
    }
    lines: list[str] = []

    # 两种「读不到数字」要分开说 —— 混成一句会把人指向错误的方向
    # （本仓老教训：一个提示说两件事，等于没说）。
    if probes.get("_redacted"):
        lines.append("⚠️  探针被隐藏 —— 请带上 --token（未鉴权调用只返回脱敏视图）")
        return 2, lines, facts
    if facts["reads_24h"] is None:
        ver = health.get("version", "?")
        lines += [
            f"⚠️  这个服务端（v{ver}）还没有写入活性探针，本脚本无从判断。",
            "   升级到带 ingest_liveness 探针的版本后再跑；",
            "   在那之前可以手工比对：库里最近有没有新增记忆 vs 你是否一直在用。",
        ]
        return 2, lines, facts

    reads, writes = facts["reads_24h"] or 0, facts["writes_24h"] or 0
    turn_w = facts["turn_writes_24h"]
    conv_r = facts["conv_reads_24h"]
    if turn_w is None:
        lines.append(f"最近 24 小时：检索 {reads} 次 · 写入 {writes} 条")
    else:
        _r = f"检索 {reads} 次" + (f"（来自对话 {conv_r} 次）" if conv_r is not None else "")
        lines.append(f"最近 24 小时：{_r} · 写入 {writes} 条（来自对话 {turn_w} 条）")

    # 读线是旧版本（不透传 session）时，既判不了写线、M2 也在空转 —— 必须
    # 说出来，不许当成「一切正常」。判据与 /health 的第三态同源。
    if (conv_r is not None and conv_r == 0 and reads >= 5
            and facts["liveness_ok"] is not False):
        lines += [
            "",
            "⚠️  无法判断写线：这 24 小时的检索没有一次带 session，",
            "   说明读线是不透传 session 的旧版本（拿到的全是定时巡检心跳）。",
            "   后果有两个：本检查失去射程，且 M2 回声抑制一直在空转",
            "   （检索侧拿不到会话，就不会排除本会话刚写入的内容）。",
            "   修法：升级 integrations/aidumem-inject.sh 后重启宿主网关。",
        ]
        return (1 if require_judgment else 0), lines, facts

    if facts["liveness_ok"] is False:
        lines += [
            "",
            "❌ 对话没有被写进记忆库 —— 你在读，但这一路没在写。",
            "",
            "   现象：检索有结果、/health 看着正常，连库里都还在新增记忆",
            "   （那是 cron 整合器和同步引擎在写），但**新对话一句都没记下来**。",
            "",
            "   两种可能，都要查：",
            "   ① 宿主只挂了注入钩子，没挂写入钩子 —— 记忆只出不进。",
            "      现成脚本：integrations/aidumem-ingest.sh（Hermes post_llm_call）",
            "      或 integrations/cursor-hook/claude-code-stop-hook.py（Claude Code Stop）",
            "   ② 挂了，但写入时没带 _origin_session_id。",
            "",
            "   挂法与验收见 docs/AGENT_INTEGRATION.md「两条线」。",
        ]
        return 1, lines, facts

    if (conv_r if conv_r is not None else reads) < 5:
        lines += ["", "ℹ️  检索次数太少，还判断不了接线是否正常 —— 正常用一阵再来看。"]
        # 「样本不足」是**无法判断**，不是「一切正常」。默认仍返回 0，
        # 免得假红灯挡住刚部署的人；但用户审计点名（🟡-3）：同一个脚本里
        # 「未鉴权」「无探针」都返回 2，唯独这里返回 0，口径不一致，而且
        # 一个刚装好就断线的系统会从 CI 门禁一路绿过去。
        # --require-judgment 让需要明确结论的场景（CI / 验收）拒绝沉默通过。
        return (2 if require_judgment else 0), lines, facts

    lines.append("")
    lines.append("✅ 读写链路都在工作。")

    # 第三条线：会话精华活性（distill_liveness）
    dis_ok = facts["distill_liveness_ok"]
    dis_sess = facts["distill_sessions_24h"]
    dis_made = facts["distill_made_24h"]
    if dis_sess is not None and dis_made is not None:
        lines.append(f"会话精华萃取：最近 24 小时经历 {dis_sess} 个会话 · 产出 {dis_made} 条精华")
        if dis_ok is False:
            lines += [
                "",
                "⚠️  第三条线（会话精华萃取）疑似未触发或挂载异常：",
                f"   最近 24h 有 {dis_sess} 个会话写入过记忆，却一条精华都没产出。",
                "   请检查 Hermes config.yaml 的 on_session_end 钩子，",
                "   确认 integrations/aidumem-distill.sh 是否配置并有可执行权限。",
            ]
            if require_judgment:
                return 1, lines, facts

    cov = facts["session_coverage"]
    if cov is not None and cov == 0:
        lines += [
            "",
            "⚠️  但写入**没带 session 标记** —— 回声抑制与轨迹学习不会生效。",
            "   （不影响记忆存取，只是这两个功能在空转。）",
            "   修法：写入时在 metadata 里带上 _origin_session_id，见 docs/INTEGRATION.md。",
        ]
    return 0, lines, facts


def main() -> int:
    ap = argparse.ArgumentParser(description="检查 aiduMEI 的记忆写入链路是否接上")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--require-judgment", action="store_true",
                    help="拒绝沉默通过：样本不足或读线旧版本时以非 0 退出。"
                         "CI / 验收场景用它 —— 默认行为不挡刚部署的新系统，"
                         "但那也意味着一个刚装好就断线的系统会一路绿过去。")
    args = ap.parse_args()

    try:
        health = _get(args.url, args.token, "/health")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)[:200]}, ensure_ascii=False))
        else:
            print(f"连不上 {args.url}/health：{exc}")
        return 2

    code, lines, facts = diagnose(health, require_judgment=args.require_judgment)
    if args.json:
        print(json.dumps({"ok": code == 0, "exit_code": code, **facts},
                         ensure_ascii=False))
    else:
        print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())

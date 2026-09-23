#!/usr/bin/env python3
"""钩子部署一致性自检 —— 宿主真正在执行的，是不是仓库这一版？

**为什么需要这个脚本**（f0.1 用户审计实锤的教训）：

宿主钩子是**拷贝不是软链**。升级仓库代码后若不重新部署，宿主执行的仍是旧文件，
且**不报任何错**——读线悄悄退回旧行为，日志干净，巡检全绿，是典型假绿灯。

更阴的一层：早期安装可能用了**别的文件名**（例如 `mem0-inject.sh`），
而集成文档写的是 `aidumem-inject.sh`。照着文档核对会验到一个宿主根本不执行的文件，
得出「已部署」的错误结论。

所以本脚本**不认文件名，只认 config.yaml**：宿主声明调哪个路径，就去比哪个路径。

用法：
    python3 scripts/check_hook_deployment.py            # 人读
    python3 scripts/check_hook_deployment.py --json     # 机读
    python3 scripts/check_hook_deployment.py --config <path> --repo <dir>

退出码：0=全部一致；1=存在漂移/缺失（可直接挂进 cron 或 CI）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

# 宿主 config.yaml 里挂钩子的事件名。新增事件时在这里加。
HOOK_EVENTS = ("pre_llm_call", "post_llm_call", "on_session_end")

# 事件 → 仓库里的真源文件。**按事件而不是按文件名认源**是本脚本的关键：
# 宿主的脚本可能是早期安装留下的别名（如 mem0-inject.sh），按文件名找会找不到，
# 于是最该报警的「改过名 + 内容是旧的」反而被降级成「无法判定」而被人放过。
EVENT_TO_SOURCE = {
    "pre_llm_call": "aidumem-inject.sh",
    "post_llm_call": "aidumem-ingest.sh",
    "on_session_end": "aidumem-distill.sh",
}

# config.yaml 中 `command:` 的值形如 "~/.hermes/agent-hooks/xxx.sh" 或带参数。
_COMMAND_RE = re.compile(r"""^\s*-?\s*command\s*:\s*["']?([^"'\n]+)["']?\s*$""")
_EVENT_RE = re.compile(r"^\s*([a-z_]+)\s*:\s*$")


def _md5(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return None


def parse_hook_commands(config_path: str) -> dict[str, list[str]]:
    """从 config.yaml 里读出每个事件实际调用的脚本路径。

    故意用正则而不是 PyYAML：本脚本要能在**任何**装了 aiduMEI 的机器上裸跑自检，
    不该因为缺一个可选依赖而失效（缺依赖时静默跳过 = 又一个假绿灯）。
    """
    found: dict[str, list[str]] = {}
    current: str | None = None
    try:
        with open(config_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return found

    for line in lines:
        ev = _EVENT_RE.match(line)
        if ev and ev.group(1) in HOOK_EVENTS:
            current = ev.group(1)
            found.setdefault(current, [])
            continue
        if ev and ev.group(1) not in HOOK_EVENTS:
            # 进入了别的段落，退出当前事件作用域
            if current and not line.startswith((" ", "\t")):
                current = None
            continue
        if current is None:
            continue
        m = _COMMAND_RE.match(line)
        if m:
            # 只取可执行文件本身，丢掉后面的参数
            cmd = m.group(1).strip().split()[0]
            found[current].append(os.path.expanduser(cmd))
    return {k: v for k, v in found.items() if v}


def check(config_path: str, repo_dir: str) -> dict:
    """比对宿主实际调用的每个脚本与仓库同名源文件的 md5。"""
    commands = parse_hook_commands(config_path)
    items = []
    for event, paths in sorted(commands.items()):
        for host_path in paths:
            host_md5 = _md5(host_path)
            # ① 先按同名找（一个事件挂多个自定义脚本时，同名最准）
            repo_path = os.path.join(repo_dir, "integrations", os.path.basename(host_path))
            repo_md5 = _md5(repo_path)
            if repo_md5 is None:
                # ② 同名找不到 —— 宿主多半用了别名。按**事件**认源，这才是权威对应。
                src = EVENT_TO_SOURCE.get(event)
                if src:
                    repo_path = os.path.join(repo_dir, "integrations", src)
                    repo_md5 = _md5(repo_path)
            if repo_md5 is None:
                # ③ 事件也不认识（自定义钩子）：退而按内容找同源文件
                repo_path, repo_md5 = _find_repo_source_by_content(repo_dir, host_md5)
            if host_md5 is None:
                status = "missing"
            elif repo_md5 is None:
                status = "unknown_source"
            elif host_md5 == repo_md5:
                status = "ok"
            else:
                status = "drift"
            items.append({
                "event": event,
                "host_path": host_path,
                "host_md5": host_md5,
                "repo_path": repo_path,
                "repo_md5": repo_md5,
                "status": status,
            })
    bad = [i for i in items if i["status"] != "ok"]
    return {
        "config": config_path,
        "repo": repo_dir,
        "items": items,
        "ok": not bad and bool(items),
        "checked": len(items),
        "drifted": len(bad),
        "no_hooks_found": not items,
    }


def _find_repo_source_by_content(repo_dir: str, host_md5: str | None):
    """宿主脚本被改过名时，按 md5 在 integrations/ 里找回它的真源。

    找得到 = 内容其实一致（只是名字不同），不算漂移；找不到才报 unknown_source。
    """
    d = os.path.join(repo_dir, "integrations")
    if host_md5 is None or not os.path.isdir(d):
        return None, None
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if os.path.isfile(p) and _md5(p) == host_md5:
            return p, host_md5
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser(description="钩子部署一致性自检")
    ap.add_argument("--config", default=os.path.expanduser("~/.hermes/config.yaml"))
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--json", action="store_true", help="输出 JSON 供机器消费")
    args = ap.parse_args()

    rep = check(args.config, args.repo)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1

    if rep["no_hooks_found"]:
        print(f"⚠️  在 {rep['config']} 里没找到任何钩子声明")
        print("    —— 要么宿主没装钩子，要么事件名不在 HOOK_EVENTS 里。")
        print("    这不是「通过」，是「没测到」。请人工确认。")
        return 1

    print(f"🔍 钩子部署一致性（依据 {rep['config']} 声明的真实路径）")
    for i in rep["items"]:
        icon = {"ok": "✅", "drift": "🔴", "missing": "🔴", "unknown_source": "⚠️"}[i["status"]]
        print(f"  {icon} {i['event']}: {i['host_path']}")
        if i["status"] == "drift":
            print(f"      宿主 {i['host_md5'][:8]} ≠ 仓库 {i['repo_md5'][:8]} ({i['repo_path']})")
            print(f"      → install -m 755 {i['repo_path']} {i['host_path']}")
        elif i["status"] == "missing":
            print("      宿主声明了这个路径，但文件不存在 —— 钩子静默失效中")
        elif i["status"] == "unknown_source":
            print("      仓库 integrations/ 里找不到对应源文件，无法判定新旧（人工确认）")

    if rep["ok"]:
        print(f"\n总计: 已比对 {rep['checked']} 个 · 🟢 宿主执行的就是本仓库这一版")
        return 0
    print(f"\n总计: 已比对 {rep['checked']} 个 · 🔴 {rep['drifted']} 个不一致 —— 升级后忘了重新部署？")
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""一条命令把 aiduMEI 接到本机 AI 宿主，并当场验收（f0.7 P3，网友反馈「装完不挂载」）。

病根：安装提示词第 9 步只有文字，全靠 agent 手改宿主配置；/health 与 e2e 全绿
也不代表宿主真接上了——服务好好的，宿主一句话都没往里写。本脚本做三件事：

1. 自动识别宿主：Claude Code（~/.claude）、Codex CLI（~/.codex）、Hermes Agent
   （~/.hermes 或 hermes 命令）；Cursor 没有可写的三线钩子，只给手工指引、不计入。
2. 备份宿主配置后幂等写入读线 / 写线 / 萃取线三条钩子（命令直接指向仓库里的
   真源脚本，不拷贝——拷贝部署升级后会悄悄跑旧文件，见 check_hook_deployment.py）。
   重复执行只更新本脚本写的那几条，别人的钩子原样保留；--uninstall 只摘本脚本的。
3. 当场验收：用刚接好的钩子脚本跑一轮合成对话（写线存一条带一次性暗号的话 →
   新会话读线召回它），验完把这条测试记忆删掉；装了 Claude Code 且给了
   --claude-test 再用 ``claude -p`` 真聊一句。

报告首行固定三态：``服务 ✅/❌ · 宿主接入 ✅/❌``；宿主没接上即退出码 1。

用法：
    python scripts/connect_host.py                 # 识别 + 接入 + 验收
    python scripts/connect_host.py --dry-run       # 只说要改什么，不动文件
    python scripts/connect_host.py --host claude-code --claude-test
    python scripts/connect_host.py --uninstall     # 摘掉本脚本写的钩子
    python scripts/connect_host.py --check         # 不改配置，只验收现状
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://127.0.0.1:8767"
MARK = "aidumem-"  # 本仓钩子脚本统一前缀；摘/换只认它

# 宿主 → (事件, 真源脚本, 超时秒)
SHELL_HOSTS = {
    "claude-code": {
        "config": "~/.claude/settings.json",
        "dir": "integrations/claude-code",
        "lines": [("UserPromptSubmit", "aidumem-cc-inject.sh", 8),
                  ("Stop", "aidumem-cc-ingest.sh", 6),
                  ("SessionEnd", "aidumem-cc-distill.sh", 10)],
    },
    "codex": {
        "config": "~/.codex/hooks.json",
        "dir": "integrations/codex",
        "lines": [("UserPromptSubmit", "aidumem-codex-inject.sh", 6),
                  ("Stop", "aidumem-codex-ingest.sh", 5),
                  ("SessionEnd", "aidumem-codex-distill.sh", 3)],
    },
}
HERMES_PLUGIN = "integrations/hermes-plugin/aidumem"

sys.path.insert(0, str(REPO))
from ducky.utils import api_auth_headers as _auth_headers  # noqa: E402  凭据单一真源


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _expand(p: str) -> Path:
    return _home() / p[2:] if p.startswith("~/") else Path(p)


# ── 识别 ────────────────────────────────────────────────────────────────
def detect_hosts() -> list[str]:
    found = []
    if (_home() / ".claude").is_dir() or shutil.which("claude"):
        found.append("claude-code")
    if (_home() / ".codex").is_dir() or shutil.which("codex"):
        found.append("codex")
    if (_home() / ".hermes").is_dir() or shutil.which("hermes"):
        found.append("hermes")
    return found


# ── 钩子命令与配置合并（纯函数，便于测试）──────────────────────────────────
def hook_command(host: str, script: str, url: str, env_file: Path) -> str:
    parts = [f"AIDUMEI_INSTALL_DIR={_q(str(REPO))}", f"AIDUMEM_ENV_FILE={_q(str(env_file))}"]
    if url and url.rstrip("/") != DEFAULT_URL:
        parts.append(f"AIDUMEI_URL={_q(url.rstrip('/'))}")
    parts.append(f"bash {_q(str(REPO / SHELL_HOSTS[host]['dir'] / script))}")
    return " ".join(parts)


def _q(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _ours(cmd: str, script: str) -> bool:
    return script in cmd


def merge_hooks(cfg: dict, host: str, url: str, env_file: Path, remove: bool = False) -> tuple[dict, list[str]]:
    """返回 (新配置, 改动说明)。只动命令里含本仓脚本名的条目，其余原样。"""
    cfg = json.loads(json.dumps(cfg or {}))
    hooks = cfg.setdefault("hooks", {})
    changes = []
    for event, script, timeout in SHELL_HOSTS[host]["lines"]:
        want = hook_command(host, script, url, env_file)
        groups = hooks.get(event) or []
        hit = False
        for grp in groups:
            kept = []
            for h in grp.get("hooks", []):
                if _ours(str(h.get("command", "")), script):
                    if remove or hit:
                        changes.append(f"摘除 {event} → {script}")
                        continue
                    hit = True
                    if h.get("command") != want or h.get("timeout") != timeout:
                        h.update({"type": "command", "command": want, "timeout": timeout})
                        changes.append(f"更新 {event} → {script}")
                kept.append(h)
            grp["hooks"] = kept
        groups = [g for g in groups if g.get("hooks")]
        if not remove and not hit:
            groups.append({"matcher": "", "hooks": [{"type": "command", "command": want, "timeout": timeout}]})
            changes.append(f"新增 {event} → {script}")
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        cfg.pop("hooks", None)
    return cfg, changes


def wired_lines(cfg: dict, host: str) -> dict[str, bool]:
    hooks = (cfg or {}).get("hooks") or {}
    out = {}
    for event, script, _ in SHELL_HOSTS[host]["lines"]:
        cmds = [str(h.get("command", "")) for g in hooks.get(event, []) for h in g.get("hooks", [])]
        out[event] = any(_ours(c, script) for c in cmds)
    return out


def _write_config(path: Path, cfg: dict) -> Path | None:
    """先备份再原子写；返回备份路径。"""
    bak = None
    if path.exists():
        bak = path.with_name(f"{path.name}.bak-aidumei-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, bak)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)
    return bak


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


# ── 各宿主接入 ──────────────────────────────────────────────────────────
def connect_shell_host(host: str, url: str, env_file: Path, dry: bool, remove: bool) -> dict:
    path = _expand(SHELL_HOSTS[host]["config"])
    try:
        cfg = _load(path)
    except Exception as exc:  # 宿主配置坏了绝不覆盖
        return {"host": host, "ok": False, "config": str(path), "error": f"配置解析失败，未改动: {exc}"}
    new, changes = merge_hooks(cfg, host, url, env_file, remove=remove)
    bak = None
    if changes and not dry:
        bak = _write_config(path, new)
    lines = wired_lines(cfg if dry else new, host)
    res = {"host": host, "config": str(path), "changes": changes, "backup": str(bak) if bak else None,
           "lines": lines, "ok": remove or all(lines.values()) or (dry and bool(changes))}
    if host == "codex" and not remove:
        res["note"] = "Codex 非 managed 钩子需在 TUI 里 /hooks 审阅信任一次；无头用 --dangerously-bypass-hook-trust"
    return res


def connect_hermes(dry: bool, remove: bool) -> dict:
    link = _home() / ".hermes" / "plugins" / "aidumem"
    src = REPO / HERMES_PLUGIN
    res = {"host": "hermes", "plugin": str(link), "changes": []}
    if remove:
        if link.is_symlink() and not dry:
            link.unlink()
        res["changes"].append("摘除插件软链（memory.provider 请用 hermes config set 改回原值）")
        res["ok"] = True
        return res
    if link.exists() and not link.is_symlink():
        res.update(ok=False, error=f"{link} 已存在且不是本脚本建的软链——拒绝覆盖")
        return res
    if not (link.is_symlink() and link.resolve() == src.resolve()):
        res["changes"].append(f"软链插件 {link} → {src}")
        if not dry:
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink():
                link.unlink()
            link.symlink_to(src)
    hermes = shutil.which("hermes")
    if hermes:
        res["changes"].append("hermes config set memory.provider aidumem")
        if not dry:
            p = subprocess.run([hermes, "config", "set", "memory.provider", "aidumem"],
                               capture_output=True, text=True, timeout=30)
            if p.returncode != 0:
                res.update(ok=False, error=f"hermes config set 失败: {p.stderr.strip()[:200]}")
                return res
        res["ok"] = True
    else:
        res.update(ok=False, error="没找到 hermes 命令，插件已就位但 memory.provider 未设置")
    return res


# ── 验收 ────────────────────────────────────────────────────────────────
def _env_lookup(env_file: Path, key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    for cand in (env_file, _home() / ".aidumem" / ".env", REPO / ".env"):
        try:
            for ln in cand.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln.startswith("export "):
                    ln = ln[7:].lstrip()
                if ln.startswith(key) and ln[len(key):].lstrip().startswith("="):
                    return ln.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return ""


def _headers(token: str = "") -> dict:
    """凭据走统一入口（ducky.utils.api_auth_headers：环境变量 → 仓库 .env）；--env-file 另给的 token 优先。"""
    return {"Authorization": "Bearer " + token} if token else _auth_headers()


def _post(url: str, path: str, body: dict, token: str, timeout: float = 10) -> dict:
    h = {"Content-Type": "application/json", **_headers(token)}
    req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def check_service(url: str, token: str) -> tuple[bool, str]:
    h = _headers(token)
    try:
        with urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/health", headers=h), timeout=5) as r:
            d = json.loads(r.read() or b"{}")
        # 与安装提示词第 7 步同一判据：health_status=ok 才算服务好（degraded 不算）
        ok = (d.get("health_status") or d.get("status")) == "ok"
        why = "" if ok else f" degraded={d.get('degraded') or []}"
        return ok, f"version={d.get('version', '?')} health={d.get('health_status', d.get('status'))}{why}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def selftest_shell(host: str, url: str, env_file: Path, wait_s: float = 60) -> dict:
    """走刚接好的真钩子脚本：写线存暗号 → 新会话读线召回 → 删掉测试记忆。"""
    d = REPO / SHELL_HOSTS[host]["dir"]
    ingest = d / SHELL_HOSTS[host]["lines"][1][1]
    inject = d / SHELL_HOSTS[host]["lines"][0][1]
    nonce = "AIDU-" + secrets.token_hex(3).upper()
    env = {**os.environ, "AIDUMEI_INSTALL_DIR": str(REPO), "AIDUMEM_ENV_FILE": str(env_file),
           "AIDUMEI_URL": url.rstrip("/")}
    with tempfile.TemporaryDirectory(prefix="aidumei-connect-") as td:
        tp = Path(td) / "transcript.jsonl"
        tp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (
            {"type": "user", "message": {"role": "user", "content": f"请记住，我的接入暗号是 {nonce}。"}},
            {"type": "assistant", "message": {"role": "assistant", "content": f"好的，记住了：你的接入暗号是 {nonce}。"}},
        )) + "\n", encoding="utf-8")
        env_log = {**env, "AIDUMEM_LOG_DIR": td}
        subprocess.run(["bash", str(ingest)], input=json.dumps({"session_id": f"selftest-w-{nonce}",
                       "transcript_path": str(tp)}), env=env_log, capture_output=True, text=True, timeout=30)
        deadline, out = time.time() + wait_s, ""
        while time.time() < deadline:
            time.sleep(3)
            p = subprocess.run(["bash", str(inject)], input=json.dumps({"session_id": f"selftest-r-{nonce}",
                               "prompt": "我的接入暗号是什么？"}), env=env_log, capture_output=True, text=True,
                               timeout=30)
            out = p.stdout
            if nonce in out:
                break
        log = (Path(td) / "aidumem-cc-ingest.log")
        err = log.read_text(errors="replace")[-300:] if log.exists() else ""
    recalled = nonce in out
    if not recalled and "add_failed" in err:
        err += "；" + _probe_add_reason(url, env_file)
    cleaned = _cleanup(url, env_file, nonce)
    return {"ok": recalled, "nonce": nonce, "cleaned": cleaned,
            "detail": "写线存入、新会话读线召回成功" if recalled else
                      f"{int(wait_s)} 秒内读线没召回暗号（写线日志: {err.strip() or '无'}）"}


def _probe_add_reason(url: str, env_file: Path) -> str:
    """写线失败时，用一次空 messages 的 /add 探服务端拒绝原因（不写入任何内容）。"""
    uid = _env_lookup(env_file, "AIDUMEM_USER_ID") or "default"
    try:
        _post(url, "/add", {"messages": [], "user_id": uid, "caller_user_id": uid}, _env_lookup(env_file, "AIDUMEM_API_TOKEN"))
        return "服务端未拒绝空写入"
    except urllib.error.HTTPError as exc:
        try:
            return f"服务端 {exc.code}: {json.loads(exc.read() or b'{}').get('detail', '')}"[:300]
        except Exception:
            return f"服务端 {exc.code}"
    except Exception as exc:
        return type(exc).__name__


def _cleanup(url: str, env_file: Path, nonce: str) -> int:
    uid = _env_lookup(env_file, "AIDUMEM_USER_ID") or _env_lookup(env_file, "AIDUMEM_DEFAULT_USER_ID") or "default"
    bid = _env_lookup(env_file, "AIDUMEI_BANK_ID") or "default"
    tok = _env_lookup(env_file, "AIDUMEM_API_TOKEN")
    n = 0
    try:
        r = _post(url, "/search", {"query": nonce, "user_id": uid, "caller_user_id": uid, "bank_id": bid,
                                   "limit": 10}, tok)
        for m in r.get("results") or []:
            if nonce in str(m.get("memory", "")) and m.get("id"):
                _post(url, "/delete", {"memory_id": m["id"], "user_id": uid, "caller_user_id": uid,
                                       "bank_id": bid}, tok)
                n += 1
    except Exception:
        pass
    return n


def claude_test(timeout: int = 120) -> dict:
    exe = shutil.which("claude")
    if not exe:
        return {"ok": False, "detail": "没找到 claude 命令"}
    try:
        p = subprocess.run([exe, "-p", "用一句话回答：你现在能看到 aiduMEI 注入的记忆吗？"],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"claude -p 超时 {timeout}s"}
    return {"ok": p.returncode == 0 and bool(p.stdout.strip()),
            "detail": (p.stdout.strip() or p.stderr.strip())[:200]}


# ── 主流程 ──────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", action="append", choices=["claude-code", "codex", "hermes"],
                    help="指定宿主（可多次）；缺省自动识别")
    ap.add_argument("--url", default=os.environ.get("AIDUMEI_URL", DEFAULT_URL))
    ap.add_argument("--env-file", default=str(REPO / ".env"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--check", action="store_true", help="不改配置，只验收")
    ap.add_argument("--no-selftest", action="store_true")
    ap.add_argument("--claude-test", action="store_true", help="另用 claude -p 真聊一句")
    ap.add_argument("--wait", type=float, default=60, help="验收等待召回的秒数")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    env_file = Path(a.env_file)
    token = _env_lookup(env_file, "AIDUMEM_API_TOKEN")
    svc_ok, svc_detail = check_service(a.url, token)
    hosts = a.host or detect_hosts()
    report = {"service": {"ok": svc_ok, "detail": svc_detail}, "hosts": [], "selftest": None, "claude": None}

    for h in hosts:
        if h == "hermes":
            report["hosts"].append(connect_hermes(dry=a.dry_run or a.check, remove=a.uninstall))
        else:
            r = connect_shell_host(h, a.url, env_file, dry=a.dry_run or a.check, remove=a.uninstall)
            if a.check:
                r["ok"] = all(r.get("lines", {}).values()) if r.get("lines") else False
            report["hosts"].append(r)

    shell_ok = [r["host"] for r in report["hosts"] if r.get("ok") and r["host"] in SHELL_HOSTS]
    if svc_ok and shell_ok and not (a.dry_run or a.uninstall or a.no_selftest):
        report["selftest"] = selftest_shell(shell_ok[0], a.url, env_file, wait_s=a.wait)
    if a.claude_test and "claude-code" in shell_ok and not (a.dry_run or a.uninstall):
        report["claude"] = claude_test()

    if a.uninstall:
        wired = None
    elif a.dry_run:
        wired = None
    else:
        need_selftest = bool(shell_ok) and not a.no_selftest
        st_ok = bool(report["selftest"] and report["selftest"]["ok"]) if need_selftest else True
        wired = svc_ok and bool(report["hosts"]) and all(r.get("ok") for r in report["hosts"]) \
            and st_ok and (report["claude"] is None or report["claude"]["ok"])
    report["wired"] = wired
    head = f"服务 {'✅' if svc_ok else '❌'} · 宿主接入 " + (
        "—（仅预览）" if a.dry_run else "—（已卸载）" if a.uninstall else ("✅" if wired else "❌"))
    report["headline"] = head

    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(head)
        print(f"  服务 {a.url}: {svc_detail}")
        if not hosts:
            print("  没识别到宿主（Claude Code / Codex / Hermes）。Cursor 等请按 docs/AGENT_INTEGRATION.md 手工接。")
        for r in report["hosts"]:
            mark = "✅" if r.get("ok") else "❌"
            print(f"  {mark} {r['host']}: {r.get('config') or r.get('plugin')}")
            for c in r.get("changes") or []:
                print(f"     · {c}")
            if r.get("backup"):
                print(f"     · 原配置已备份: {r['backup']}")
            for k in ("error", "note"):
                if r.get(k):
                    print(f"     ! {r[k]}")
        if report["selftest"]:
            s = report["selftest"]
            print(f"  {'✅' if s['ok'] else '❌'} 当场验收（{s['nonce']}）: {s['detail']}；清理测试记忆 {s['cleaned']} 条")
        if report["claude"]:
            c = report["claude"]
            print(f"  {'✅' if c['ok'] else '❌'} claude -p: {c['detail']}")
    if a.dry_run or a.uninstall:
        return 0 if all(r.get("ok") for r in report["hosts"]) else 1
    return 0 if (svc_ok and wired) else 1


if __name__ == "__main__":
    sys.exit(main())

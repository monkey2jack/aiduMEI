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
    python scripts/connect_host.py --host claude-code --project-dir /path/to/proj  # 写项目级 .claude/settings.json
    python scripts/connect_host.py --user-id alice --allow-dir /work/alice     # 身份写死 + 写入白名单

f0.8+（外审 R3-01 / P0-1）：钩子命令里**显式写上** ``AIDUMEM_USER_ID``（--user-id；缺省取环境变量或
--env-file 里的 AIDUMEM_USER_ID / AIDUMEM_DEFAULT_USER_ID），钩子不再靠项目目录的 .env 决定身份；
--allow-dir（可多次）写 ``AIDUMEI_CC_ALLOW_DIRS``：会话目录不在其下时 Claude Code 钩子不写入。
--check 会提示：身份没写死的老钩子、白名单没设、.env 里误留了测试开关。

Claude Code 配置位置（f0.8.0）：设了 ``CLAUDE_CONFIG_DIR`` 就用
``$CLAUDE_CONFIG_DIR/settings.json``（与 cc_memory_bridge.claude_home 同一规则），否则
``~/.claude/settings.json``；``--project-dir`` 改写/验收该项目的 ``.claude/settings.json``。
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

# 宿主 → (事件, 真源脚本, 超时秒[, matcher])；matcher 缺省 ""（全部）
# f0.8 ①：Claude Code 多一条 PostToolUse 自带记忆桥（只认 Write|Edit|MultiEdit），
# 把 CC 写进 ~/.claude/projects/<项目>/memory/ 的文件打码并导入 aiduMEI。
# 超时 30 秒：桥里查重 + 写入两次请求，每次最多 AIDUMEI_CC_BRIDGE_TIMEOUT（缺省 12）秒（f0.8.0 上线实测生产 /search 4–6 秒）
# f0.8+：matcher 去掉 MultiEdit（新版 CC 已无此工具；桥本身仍认 MultiEdit 载荷）。老安装的
# "Write|Edit|MultiEdit" 在 --check 里照样算挂上，重跑安装会换成新 matcher。
CC_BRIDGE = ("PostToolUse", "aidumem-cc-memory-bridge.sh", 30, "Write|Edit")
LEGACY_MATCHERS = {"Write|Edit": ("Write|Edit|MultiEdit",)}
# P0-1：钩子读的开关（--check 报告它们在 --env-file 里设没设；不打印值）
HOOK_SWITCHES = ("AIDUMEI_CC_ALLOW_DIRS", "AIDUMEI_CC_MEMORY_ROOTS", "AIDUMEI_HOOKS",
                 "AIDUMEI_TEST_URL", "AIDUMEI_TEST_TOKEN")
SHELL_HOSTS = {
    "claude-code": {
        "config": "~/.claude/settings.json",
        "dir": "integrations/claude-code",
        "lines": [("UserPromptSubmit", "aidumem-cc-inject.sh", 8),
                  ("Stop", "aidumem-cc-ingest.sh", 6),
                  ("SessionEnd", "aidumem-cc-distill.sh", 10),
                  CC_BRIDGE],
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


def claude_home() -> Path:
    """CC 用户级配置目录：``CLAUDE_CONFIG_DIR`` 优先，否则 ``~/.claude``（同 cc_memory_bridge.claude_home）。"""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else _home() / ".claude"


def host_config_path(host: str, project_dir: str | Path | None = None) -> Path:
    """宿主配置文件路径。claude-code 认 ``--project-dir`` 与 ``CLAUDE_CONFIG_DIR``；其余宿主不变。"""
    if host == "claude-code":
        if project_dir:
            return Path(project_dir).expanduser() / ".claude" / "settings.json"
        return claude_home() / "settings.json"
    return _expand(SHELL_HOSTS[host]["config"])


# ── 识别 ────────────────────────────────────────────────────────────────
def detect_hosts() -> list[str]:
    found = []
    if claude_home().is_dir() or (_home() / ".claude").is_dir() or shutil.which("claude"):
        found.append("claude-code")
    if (_home() / ".codex").is_dir() or shutil.which("codex"):
        found.append("codex")
    if (_home() / ".hermes").is_dir() or shutil.which("hermes"):
        found.append("hermes")
    return found


# ── 钩子命令与配置合并（纯函数，便于测试）──────────────────────────────────
def hook_command(host: str, script: str, url: str, env_file: Path, user_id: str = "",
                 allow_dirs: str = "") -> str:
    parts = [f"AIDUMEI_INSTALL_DIR={_q(str(REPO))}", f"AIDUMEM_ENV_FILE={_q(str(env_file))}"]
    if user_id:  # R3-01：身份写死在用户级配置里，不留给项目目录的 .env 决定
        parts.append(f"AIDUMEM_USER_ID={_q(user_id)}")
    if allow_dirs and host == "claude-code":  # P0-1：写入白名单
        parts.append(f"AIDUMEI_CC_ALLOW_DIRS={_q(allow_dirs)}")
    if url and url.rstrip("/") != DEFAULT_URL:
        parts.append(f"AIDUMEI_URL={_q(url.rstrip('/'))}")
    parts.append(f"bash {_q(str(REPO / SHELL_HOSTS[host]['dir'] / script))}")
    return " ".join(parts)


def _q(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _ours(cmd: str, script: str) -> bool:
    return script in cmd


def _line(spec: tuple) -> tuple[str, str, int, str]:
    event, script, timeout = spec[:3]
    return event, script, timeout, (spec[3] if len(spec) > 3 else "")


def _matcher_ok(have: str, want: str) -> bool:
    return have == want or have in LEGACY_MATCHERS.get(want, ())


def merge_hooks(cfg: dict, host: str, url: str, env_file: Path, remove: bool = False,
                user_id: str = "", allow_dirs: str = "") -> tuple[dict, list[str]]:
    """返回 (新配置, 改动说明)。只动命令里含本仓脚本名的条目，其余原样。"""
    cfg = json.loads(json.dumps(cfg or {}))
    hooks = cfg.setdefault("hooks", {})
    changes = []
    for event, script, timeout, matcher in map(_line, SHELL_HOSTS[host]["lines"]):
        want = hook_command(host, script, url, env_file, user_id=user_id, allow_dirs=allow_dirs)
        groups = hooks.get(event) or []
        hit = False
        for grp in groups:
            kept = []
            for h in grp.get("hooks", []):
                if _ours(str(h.get("command", "")), script):
                    if remove or hit:
                        changes.append(f"摘除 {event} → {script}")
                        continue
                    if (grp.get("matcher") or "") != matcher:  # 挂错 matcher：摘下，下面按正确 matcher 重挂
                        changes.append(f"更新 {event} → {script}（matcher）")
                        continue
                    hit = True
                    if h.get("command") != want or h.get("timeout") != timeout:
                        h.update({"type": "command", "command": want, "timeout": timeout})
                        changes.append(f"更新 {event} → {script}")
                kept.append(h)
            grp["hooks"] = kept
        groups = [g for g in groups if g.get("hooks")]
        if not remove and not hit:
            groups.append({"matcher": matcher, "hooks": [{"type": "command", "command": want, "timeout": timeout}]})
            if not any(c.startswith(f"更新 {event} → {script}") for c in changes):
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
    for event, script, _, matcher in map(_line, SHELL_HOSTS[host]["lines"]):
        cmds = [str(h.get("command", "")) for g in hooks.get(event, [])
                if _matcher_ok(g.get("matcher") or "", matcher) for h in g.get("hooks", [])]
        out[event] = any(_ours(c, script) for c in cmds)
    return out


def cc_bridge_status(cfg: dict) -> dict:
    """f0.8 ①：CC 自带记忆桥钩子是否挂上（PostToolUse + 正确 matcher + 指向本仓真源）。"""
    event, script, _, matcher = _line(CC_BRIDGE)
    src = REPO / SHELL_HOSTS["claude-code"]["dir"] / script
    wired = wired_lines(cfg, "claude-code").get(event, False)
    ours = [str(h.get("command", "")) for g in ((cfg or {}).get("hooks") or {}).get(event, [])
            if _matcher_ok(g.get("matcher") or "", matcher)
            for h in g.get("hooks", []) if _ours(str(h.get("command", "")), script)]
    points_here = any(str(src) in c for c in ours)
    return {"event": event, "matcher": matcher, "wired": bool(wired and points_here),
            "script_present": src.is_file(),
            "detail": "已挂上" if wired and points_here else
                      ("已挂但指向别处或 matcher 不对" if ours else "未挂（重跑 connect_host 即可补上）")}


def identity_pinned(cfg: dict, host: str) -> dict[str, bool]:
    """R3-01：本仓每条钩子命令是否显式写了 AIDUMEM_USER_ID（没写 = 身份靠 .env 兜底）。"""
    hooks = (cfg or {}).get("hooks") or {}
    out = {}
    for event, script, _, _m in map(_line, SHELL_HOSTS[host]["lines"]):
        cmds = [str(h.get("command", "")) for g in hooks.get(event, []) for h in g.get("hooks", [])
                if _ours(str(h.get("command", "")), script)]
        if cmds:
            out[event] = all("AIDUMEM_USER_ID=" in c for c in cmds)
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
def _existing_assign(cfg: dict, host: str, key: str) -> str:
    """本仓钩子命令里已写的 ``KEY=值``（重跑安装不给参数时沿用，不悄悄换身份、丢白名单）。"""
    import shlex
    scripts = [script for _e, script, _t, _m in map(_line, SHELL_HOSTS[host]["lines"])]
    for groups in ((cfg or {}).get("hooks") or {}).values():
        for g in groups or []:
            for h in g.get("hooks", []):
                cmd = str(h.get("command", ""))
                if not any(_ours(cmd, sc) for sc in scripts):
                    continue
                try:
                    toks = shlex.split(cmd)
                except ValueError:
                    continue
                # 只认命令开头的 KEY=值 前缀（手改成 case/if 的命令不猜），值也得是安全字符
                for t in toks:
                    k, eq, v = t.partition("=")
                    if not eq or not k.replace("_", "").isalnum() or not k.isupper():
                        break
                    if k == key and v and all(_safe_id(x) or x.startswith("/") for x in v.split(":")):
                        return v
    return ""


def connect_shell_host(host: str, url: str, env_file: Path, dry: bool, remove: bool,
                       project_dir: str | Path | None = None, user_id: str = "",
                       allow_dirs: str | None = None, default_user_id: str = "") -> dict:
    """user_id：显式给的身份；没给就沿用已装命令里的，再没有用 default_user_id。
    allow_dirs：None = 沿用已装命令里的白名单（没有就不写）；字符串 = 用它。"""
    path = host_config_path(host, project_dir)
    try:
        cfg = _load(path)
    except Exception as exc:  # 宿主配置坏了绝不覆盖
        return {"host": host, "ok": False, "config": str(path), "error": f"配置解析失败，未改动: {exc}"}
    user_id = user_id or _existing_assign(cfg, host, "AIDUMEM_USER_ID") or default_user_id
    if allow_dirs is None:
        allow_dirs = _existing_assign(cfg, host, "AIDUMEI_CC_ALLOW_DIRS") if host == "claude-code" else ""
    new, changes = merge_hooks(cfg, host, url, env_file, remove=remove, user_id=user_id,
                               allow_dirs=allow_dirs)
    bak = None
    if changes and not dry:
        bak = _write_config(path, new)
    lines = wired_lines(cfg if dry else new, host)
    res = {"host": host, "config": str(path), "changes": changes, "backup": str(bak) if bak else None,
           "lines": lines, "ok": remove or all(lines.values()) or (dry and bool(changes))}
    res["user_id"], res["allow_dirs"] = user_id, allow_dirs
    if not remove:
        pinned = identity_pinned(cfg if dry else new, host)
        res["identity_pinned"] = bool(pinned) and all(pinned.values())
    if host == "claude-code":
        res["cc_memory_bridge"] = cc_bridge_status(cfg if dry else new)
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


def _hook_env(env_file: Path, url: str, user_id: str = "", allow_dirs: str = "") -> dict:
    """验收时调钩子用的环境：与写进宿主配置的命令同一套（身份写死、白名单同值）。"""
    env = {**os.environ, "AIDUMEI_INSTALL_DIR": str(REPO), "AIDUMEM_ENV_FILE": str(env_file),
           "AIDUMEM_URL": url.rstrip("/"), "AIDUMEI_URL": url.rstrip("/")}
    if user_id:
        env["AIDUMEM_USER_ID"] = user_id
    if allow_dirs:
        env["AIDUMEI_CC_ALLOW_DIRS"] = allow_dirs
    return env


def _probe_cwd(allow_dirs: str) -> str:
    """验收载荷里的会话目录：白名单第一个存在的目录（不在白名单里写线会按设计跳过）。"""
    for d in (allow_dirs or "").split(":"):
        if d and Path(d).expanduser().is_dir():
            return str(Path(d).expanduser())
    return os.getcwd()


def _run_ingest(ingest: Path, payload: dict, env: dict) -> None:
    """跑写线钩子并等它写完：CC 写线平时读完 stdin 就把活交给后台（f0.8+），验收要的是
    「写完再查」，所以直接走它的后台入口（AIDUMEI_CC_HOOK_BG=1，载荷经 AIDUMEI_HOOK_INPUT）。"""
    raw = json.dumps(payload)
    subprocess.run(["bash", str(ingest)], input=raw, capture_output=True, text=True, timeout=60,
                   env={**env, "AIDUMEI_CC_HOOK_BG": "1", "AIDUMEI_HOOK_INPUT": raw})


def selftest_shell(host: str, url: str, env_file: Path, wait_s: float = 60, user_id: str = "",
                   allow_dirs: str = "") -> dict:
    """走刚接好的真钩子脚本：写线存暗号 → 新会话读线召回 → 删掉测试记忆。"""
    d = REPO / SHELL_HOSTS[host]["dir"]
    ingest = d / SHELL_HOSTS[host]["lines"][1][1]
    inject = d / SHELL_HOSTS[host]["lines"][0][1]
    nonce = "AIDU-" + secrets.token_hex(3).upper()
    env = _hook_env(env_file, url, user_id, allow_dirs)
    cwd = _probe_cwd(allow_dirs)
    with tempfile.TemporaryDirectory(prefix="aidumei-connect-") as td:
        tp = Path(td) / "transcript.jsonl"
        tp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (
            {"type": "user", "message": {"role": "user", "content": f"请记住，我的接入暗号是 {nonce}。"}},
            {"type": "assistant", "message": {"role": "assistant", "content": f"好的，记住了：你的接入暗号是 {nonce}。"}},
        )) + "\n", encoding="utf-8")
        env_log = {**env, "AIDUMEM_LOG_DIR": td}
        _run_ingest(ingest, {"session_id": f"selftest-w-{nonce}", "transcript_path": str(tp), "cwd": cwd},
                    env_log)
        deadline, out = time.time() + wait_s, ""
        while time.time() < deadline:
            time.sleep(3)
            p = subprocess.run(["bash", str(inject)], input=json.dumps({"session_id": f"selftest-r-{nonce}",
                               "prompt": "我的接入暗号是什么？", "cwd": cwd}), env=env_log,
                               capture_output=True, text=True, timeout=30)
            out = p.stdout
            if nonce in out:
                break
        log = (Path(td) / "aidumem-cc-ingest.log")
        err = log.read_text(errors="replace")[-300:] if log.exists() else ""
    recalled = nonce in out
    if not recalled and "add_failed" in err:
        err += "；" + _probe_add_reason(url, env_file, user_id)
    cleaned = _cleanup(url, env_file, nonce, user_id)
    return {"ok": recalled, "nonce": nonce, "cleaned": cleaned,
            "detail": "写线存入、新会话读线召回成功" if recalled else
                      f"{int(wait_s)} 秒内读线没召回暗号（写线日志: {err.strip() or '无'}）"}


def _uid(env_file: Path, user_id: str = "") -> str:
    return user_id or _env_lookup(env_file, "AIDUMEM_USER_ID") or _env_lookup(env_file, "AIDUMEM_DEFAULT_USER_ID") \
        or "default"


def _probe_add_reason(url: str, env_file: Path, user_id: str = "") -> str:
    """写线失败时，用一次空 messages 的 /add 探服务端拒绝原因（不写入任何内容）。"""
    uid = _uid(env_file, user_id)
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


def _cleanup(url: str, env_file: Path, nonce: str, user_id: str = "") -> int:
    uid = _uid(env_file, user_id)
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


def _ingest_probe(host: str, url: str, env_file: Path, nonce: str, user_id: str = "",
                  allow_dirs: str = "", *, lookup_id: str = "") -> None:
    """用真写线钩子存一条带暗号的合成对话（与 selftest_shell 同一写法）。"""
    d = REPO / SHELL_HOSTS[host]["dir"]
    ingest = d / SHELL_HOSTS[host]["lines"][1][1]
    env = _hook_env(env_file, url, user_id, allow_dirs)
    with tempfile.TemporaryDirectory(prefix="aidumei-claude-test-") as td:
        tp = Path(td) / "transcript.jsonl"
        prefix = f"验收项目 {lookup_id}。" if lookup_id else ""
        tp.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (
            {"type": "user", "message": {"role": "user", "content": prefix + f"请记住，我的接入暗号是 {nonce}。"}},
            {"type": "assistant", "message": {"role": "assistant", "content": prefix + f"好的，记住了：你的接入暗号是 {nonce}。"}},
        )) + "\n", encoding="utf-8")
        _run_ingest(ingest, {"session_id": f"claude-test-w-{nonce}", "transcript_path": str(tp),
                             "cwd": _probe_cwd(allow_dirs)}, {**env, "AIDUMEM_LOG_DIR": td})


def _searchable(url: str, env_file: Path, nonce: str, wait_s: float, user_id: str = "") -> bool:
    uid = _uid(env_file, user_id)
    bid = _env_lookup(env_file, "AIDUMEI_BANK_ID") or "default"
    tok = _env_lookup(env_file, "AIDUMEM_API_TOKEN")
    deadline = time.time() + wait_s
    while True:
        try:
            r = _post(url, "/search", {"query": nonce, "user_id": uid, "caller_user_id": uid, "bank_id": bid,
                                       "limit": 10}, tok)
            if any(nonce in str(m.get("memory", "")) for m in r.get("results") or []):
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(3)


def claude_test(url: str, env_file: Path, timeout: int = 120, wait_s: float = 60, user_id: str = "",
                allow_dirs: str = "", model: str = "") -> dict:
    """``claude -p`` 真聊一句，**回答里必须出现刚写进去的暗号**才算过（f0.8.0）。

    f0.7 只看退出码 0 + 有输出：Claude 回一句「看不到任何记忆」也判绿。现在先用写线
    存一条一次性暗号，等服务能搜到它，再问 claude「我的接入暗号是什么」——暗号出现在
    回答里，才证明读线钩子真把记忆注入了 Claude。验完删掉测试记忆。
    """
    exe = shutil.which("claude")
    if not exe:
        return {"ok": False, "detail": "没找到 claude 命令"}
    nonce = "AIDU-" + secrets.token_hex(3).upper()
    lookup_id = "PROBE-" + secrets.token_hex(8)
    try:
        _ingest_probe("claude-code", url, env_file, nonce, user_id, allow_dirs, lookup_id=lookup_id)
        if not _searchable(url, env_file, nonce, wait_s, user_id):
            return {"ok": False, "nonce": nonce,
                    "detail": f"{int(wait_s)} 秒内服务端搜不到写入的暗号，未调用 claude -p"}
        # The probe writer and the actual CLI must use the same endpoint and
        # identity.  An inherited AIDUMEM_URL takes precedence over its alias
        # inside the hooks, so _hook_env pins both names.
        # Put the positional prompt before variadic tool flags, and also deny
        # MCP tools: --tools alone controls only Claude's built-in tools.
        # Ask about this probe's independent lookup label, never its answer;
        # old retained probes must not compete under one generic question.
        prompt = f"验收项目 {lookup_id} 的接入暗号是什么？只回答暗号本身。"
        args = [exe, "-p", prompt,
                "--tools", "", "--disallowedTools", "mcp__*"]
        if model:
            args += ["--model", model]
        try:
            p = subprocess.run(args, env=_hook_env(env_file, url, user_id, allow_dirs),
                               capture_output=True, text=True, timeout=timeout,
                               cwd=_probe_cwd(allow_dirs))
        except subprocess.TimeoutExpired:
            return {"ok": False, "nonce": nonce, "detail": f"claude -p 超时 {timeout}s"}
        out = p.stdout or ""
        recalled = p.returncode == 0 and nonce in out
        why = (f"退出码 {p.returncode}；" if p.returncode else "") + "回答里没有本次验收标记"
        return {"ok": recalled, "nonce": nonce,
                "detail": "回答里召回了本次验收标记" if recalled else why}
    finally:
        _cleanup(url, env_file, nonce, user_id)


def _safe_id(uid: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,128}", uid or "")) and not uid.startswith(".")


def switch_report(env_file: Path, allow_dirs: str) -> dict:
    """P0-1：钩子开关现状（只报设没设，不打印值）。测试开关留在常驻配置里 = 生产钩子被导走，记警告。"""
    seen = {k: bool(_env_lookup(env_file, k)) for k in HOOK_SWITCHES}
    seen["AIDUMEI_CC_ALLOW_DIRS"] = bool(allow_dirs)
    warnings = []
    if not allow_dirs:
        warnings.append("没设写入白名单（AIDUMEI_CC_ALLOW_DIRS / --allow-dir）：任何目录里的 Claude Code 会话都会写入")
    for k in ("AIDUMEI_TEST_URL", "AIDUMEI_TEST_TOKEN"):
        if seen[k]:
            warnings.append(f"{k} 出现在常驻配置里：测试开关只该在跑测试的那条命令里临时设")
    if _env_lookup(env_file, "AIDUMEI_HOOKS").strip().lower() in ("off", "0", "false", "no"):
        warnings.append("AIDUMEI_HOOKS=off：全部钩子停用中")
    return {"set": seen, "warnings": warnings}


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
    ap.add_argument("--project-dir", default=None,
                    help="Claude Code：写/验收该项目的 .claude/settings.json（缺省用户级配置）")
    ap.add_argument("--user-id", default=None,
                    help="写进钩子命令的身份（AIDUMEM_USER_ID）；缺省取环境变量或 --env-file 里的值")
    ap.add_argument("--allow-dir", action="append", default=None,
                    help="Claude Code 写入白名单目录（可多次；写进 AIDUMEI_CC_ALLOW_DIRS）")
    ap.add_argument("--claude-test", action="store_true", help="另用 claude -p 真聊一句")
    ap.add_argument("--claude-model", default="", help="CLI 验收使用的模型；不指定时沿用宿主配置")
    ap.add_argument("--wait", type=float, default=60, help="验收等待召回的秒数")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    env_file = Path(a.env_file)
    token = _env_lookup(env_file, "AIDUMEM_API_TOKEN")
    explicit_uid = (a.user_id or "").strip()
    default_uid = _uid(env_file)
    for u in (explicit_uid, default_uid):
        if u and not _safe_id(u):
            print(f"身份只许字母数字和 ._-：{u!r}", file=sys.stderr)
            return 2
    # --allow-dir 没给：None = 沿用已装命令里的白名单（钩子另从 --env-file 读 AIDUMEI_CC_ALLOW_DIRS）
    allow_arg = None if a.allow_dir is None else ":".join(
        str(Path(d).expanduser().resolve()) for d in a.allow_dir if d.strip())
    svc_ok, svc_detail = check_service(a.url, token)
    hosts = a.host or detect_hosts()
    report = {"service": {"ok": svc_ok, "detail": svc_detail}, "hosts": [], "selftest": None, "claude": None}

    for h in hosts:
        if h == "hermes":
            report["hosts"].append(connect_hermes(dry=a.dry_run or a.check, remove=a.uninstall))
        else:
            r = connect_shell_host(h, a.url, env_file, dry=a.dry_run or a.check, remove=a.uninstall,
                                   project_dir=a.project_dir, user_id=explicit_uid, allow_dirs=allow_arg,
                                   default_user_id=default_uid)
            if a.check:
                r["ok"] = all(r.get("lines", {}).values()) if r.get("lines") else False
                if h == "claude-code" and all(r.get("lines", {}).get(event) for event in
                                               ("UserPromptSubmit", "Stop", "SessionEnd")):
                    r["ok"] = True
                    if not r.get("cc_memory_bridge", {}).get("wired"):
                        r["warnings"] = ["自带记忆桥未接好：备份配置后补装；原有读、写、萃取三线仍可用"]
            report["hosts"].append(r)

    shell_ok = [r["host"] for r in report["hosts"] if r.get("ok") and r["host"] in SHELL_HOSTS]
    first = next((r for r in report["hosts"] if r["host"] in SHELL_HOSTS), {})
    user_id = first.get("user_id") or explicit_uid or default_uid
    allow_dirs = next((r.get("allow_dirs") for r in report["hosts"] if r["host"] == "claude-code"
                       and r.get("allow_dirs")), "") or _env_lookup(env_file, "AIDUMEI_CC_ALLOW_DIRS")
    if svc_ok and shell_ok and not (a.dry_run or a.uninstall or a.no_selftest):
        report["selftest"] = selftest_shell(shell_ok[0], a.url, env_file, wait_s=a.wait, user_id=user_id,
                                            allow_dirs=allow_dirs)
    if a.claude_test and "claude-code" in shell_ok and not (a.dry_run or a.uninstall):
        report["claude"] = claude_test(a.url, env_file, wait_s=a.wait, user_id=user_id,
                                      allow_dirs=allow_dirs, model=a.claude_model)

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
    report["switches"] = switch_report(env_file, allow_dirs)
    report["warnings"] = [w for r in report["hosts"] for w in r.get("warnings", [])]
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
            br = r.get("cc_memory_bridge")
            if br:
                print(f"     {'✅' if br['wired'] else '⚠'} 自带记忆桥 {br['event']}({br['matcher']}): {br['detail']}")
            for warning in r.get("warnings", []):
                print(f"     ⚠ {warning}")
            if r.get("identity_pinned") is False and r["host"] in SHELL_HOSTS:
                print(f"     ⚠ 钩子命令没写死身份（AIDUMEM_USER_ID）；重跑本脚本（可加 --user-id {user_id}）即可补上")
            for k in ("error", "note"):
                if r.get(k):
                    print(f"     ! {r[k]}")
        for w in report["switches"]["warnings"]:
            print(f"  ⚠ {w}")
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

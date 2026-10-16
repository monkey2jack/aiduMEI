#!/usr/bin/env python3
"""aiduMEI cc 侧一键体检（f0.4++ WP8）。

把 cc 侧部署后的全面自检固化为可重跑脚本：服务健康、Qdrant、
journal 证据链、hooks 三链路、守护、版本对齐、磁盘与数据规模，
一条命令输出全套体检结果。退出码 0=全绿；1=有红项。

用法：
    cd <aiduMEI 安装目录>
    venv/bin/python scripts/selfcheck_cc.py            # 人读
    venv/bin/python scripts/selfcheck_cc.py --json     # 机读
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

API_URL = os.environ.get("AIDUMEI_URL", "http://127.0.0.1:8767").rstrip("/")
HOOKS_DIR = Path(os.environ.get("CLAUDE_HOOKS_DIR",
                                  os.path.expanduser("~/.claude/hooks")))
SETTINGS = Path(os.environ.get("CLAUDE_SETTINGS",
                               os.path.expanduser("~/.claude/settings.json")))
QDRANT = os.environ.get("AIDUMEI_QDRANT_URL", "http://127.0.0.1:6333")


def _http(url: str, timeout: float = 5.0, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        return None, b""


def _token() -> str:
    env = _REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("AIDUMEM_API_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def check_api(results: list):
    tok = _token()
    st, body = _http(f"{API_URL}/health", headers={"Authorization": f"Bearer {tok}"})
    if st != 200:
        results.append(("❌", "API /health", f"http {st}（服务不通）"))
        return
    try:
        d = json.loads(body)
    except Exception:
        results.append(("❌", "API /health", "响应不是合法 JSON"))
        return
    degraded = d.get("degraded") or []
    sha = d.get("git_sha", "?")
    ver = d.get("full_version") or d.get("version") or "?"
    # 版本对齐：git_sha vs 仓库 HEAD
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        head = "?"
    aligned = head.startswith(sha) or sha.startswith(head[:7]) if sha != "?" else False
    results.append(("✅", "API /health", f"ok · {ver} · degraded={degraded or '[]'}"))
    results.append(("✅" if aligned else "❌", "版本对齐",
                    f"/health git_sha={sha[:12]} vs git HEAD={head[:12]}"))
    # journal integrity（health 探针里带）
    probes = d.get("probes") or {}
    inj = probes.get("injection_rejections") or {}
    if inj:
        results.append(("✅", "注入守卫", f"mode={inj.get('guard_mode')} rejected={inj.get('rejected_total')}"))


def check_qdrant(results: list):
    st, body = _http(f"{QDRANT}/collections")
    if st != 200:
        results.append(("❌", "Qdrant", f"http {st}"))
        return
    try:
        cols = json.loads(body)["result"]["collections"]
    except Exception:
        results.append(("❌", "Qdrant", "collections 响应解析失败"))
        return
    parts = []
    for c in cols:
        name = c["name"]
        st2, b2 = _http(f"{QDRANT}/collections/{name}")
        try:
            n = json.loads(b2)["result"]["points_count"]
        except Exception:
            n = "?"
        parts.append(f"{name}={n}")
    results.append(("✅", "Qdrant collections", " · ".join(parts)))
    # 容器 restart 策略
    try:
        pol = subprocess.run(
            ["docker", "inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}", "qdrant-server"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        results.append(("✅" if pol and pol != "no" else "⚠️", "qdrant 容器 restart 策略", pol or "unknown"))
    except Exception:
        results.append(("⚠️", "qdrant 容器 restart 策略", "docker 不可用或容器不存在"))


def check_journal(results: list):
    from ducky.mutation_journal import journal_path, journal_identity_path
    jp, ip = journal_path(), journal_identity_path(journal_path())
    if not jp.exists():
        results.append(("⚠️", "mutation journal", "库不存在（首次安装态）"))
        return
    if not ip.exists():
        results.append(("❌", "mutation journal", "identity 标记缺失——重启会 fail-closed，跑 scripts/adopt_mutation_journal.py"))
        return
    try:
        conn = sqlite3.connect(jp)
        n = conn.execute("SELECT COUNT(*) FROM mutations").fetchone()[0]
        ident_ok = conn.execute(
            "SELECT COUNT(*) FROM journal_identity").fetchone()[0] == 1
        conn.close()
        marker = json.loads(ip.read_text())
        results.append(("✅" if ident_ok else "❌", "mutation journal",
                        f"{n} 条记录 · identity 配对{'一致' if ident_ok else '异常'} · marker={marker.get('identity','?')[:8]}"))
    except Exception as e:
        results.append(("❌", "mutation journal", f"{type(e).__name__}: {e}"))


def check_hooks(results: list):
    if not SETTINGS.exists():
        results.append(("⚠️", "cc hooks", f"{SETTINGS} 不存在（非 cc 宿主？）"))
        return
    try:
        cfg = json.loads(SETTINGS.read_text())
    except Exception as e:
        results.append(("❌", "cc hooks", f"settings.json 解析失败: {e}"))
        return
    hooks = cfg.get("hooks") or {}
    # 事件 → 应挂载的真源脚本名（integrations/claude-code/ 入仓版）。
    # 宿主是拷贝部署：这里只认 settings.json 声明的命令里含不含真源名。
    expect = {"UserPromptSubmit": "aidumem-cc-inject.sh",
              "Stop": "aidumem-cc-ingest.sh",
              "SessionEnd": "aidumem-cc-distill.sh"}
    for event, script in expect.items():
        cmds = [h.get("command", "") for grp in hooks.get(event, []) for h in grp.get("hooks", [])]
        hit = any(script in c for c in cmds)
        results.append(("✅" if hit else "❌", f"hook {event}", script if hit else f"未挂载（期望 {script}）"))
        if hit:
            p = HOOKS_DIR / script
            results.append(("✅" if p.exists() else "❌", f"  脚本存在", str(p)))
    # SessionEnd 萃取语义（WP4 判据）：适配层必须复用 distill 真源，
    # 且绝不允许回到旧病灶 /session/start（重复建会话、提炼从未发生）
    se = HOOKS_DIR / "aidumem-cc-distill.sh"
    if se.exists():
        content = se.read_text()
        if "/session/start" in content:
            results.append(("❌", "SessionEnd 语义", "仍在调 /session/start（重复建会话，WP4 未修）"))
        elif "aidumem-distill.sh" in content:
            results.append(("✅", "SessionEnd 语义", "复用 distill 真源（提炼 → 落库两步走）"))
        else:
            results.append(("⚠️", "SessionEnd 语义", "未识别到 distill 真源复用"))
    # inject hook 探活超时（WP7 判据）
    inj = HOOKS_DIR / "aidumem-cc-inject.sh"
    if inj.exists():
        content = inj.read_text()
        if 'curl -s -m 0.3' in content:
            results.append(("⚠️", "inject 探活超时", "仍为 0.3s（WP7 未放宽）"))
        else:
            results.append(("✅", "inject 探活超时", "已放宽（无 0.3s 硬编码）"))


def check_watchdog(results: list):
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        out = ""
    if "aidumei_watchdog" in out:
        results.append(("✅", "守护 crontab", "已挂载 aidumei_watchdog.sh"))
    else:
        results.append(("❌", "守护 crontab", "未挂载（服务死了没人拉起）"))
    wlog = _REPO_ROOT / "logs" / "watchdog.log"
    if wlog.exists():
        results.append(("ℹ️", "守护日志", f"{wlog} ({wlog.stat().st_size}B)"))


def check_disk_data(results: list):
    try:
        st = os.statvfs("/data")
        free_gb = st.f_bavail * st.f_frsize / 1e9
        total_gb = st.f_blocks * st.f_frsize / 1e9
        pct = (1 - st.f_bavail / st.f_blocks) * 100
        results.append(("✅" if pct < 85 else "⚠️", "磁盘 /data",
                        f"已用 {pct:.0f}% · 剩余 {free_gb:.1f}G/{total_gb:.0f}G"))
    except Exception:
        pass
    data = _REPO_ROOT / "data"
    if data.exists():
        size = sum(f.stat().st_size for f in data.rglob("*") if f.is_file())
        results.append(("ℹ️", "data 目录", f"{size/1e6:.0f}MB"))


def check_fastembed(results: list):
    try:
        import fastembed  # noqa: F401
        results.append(("✅", "fastembed", "已安装（dual_index_local 可用）"))
    except ImportError:
        results.append(("⚠️", "fastembed", "未安装——dual_index_local 欠账会循环重试"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results: list = []
    t0 = time.time()
    check_api(results)
    check_qdrant(results)
    check_journal(results)
    check_hooks(results)
    check_watchdog(results)
    check_fastembed(results)
    check_disk_data(results)
    elapsed = time.time() - t0

    red = [r for r in results if r[0] == "❌"]
    yellow = [r for r in results if r[0] == "⚠️"]

    if args.json:
        print(json.dumps({
            "ts": time.time(), "elapsed_s": round(elapsed, 2),
            "red": len(red), "yellow": len(yellow),
            "items": [{"mark": m, "name": n, "detail": d} for m, n, d in results],
        }, ensure_ascii=False, indent=1))
    else:
        print(f"═══ aiduMEI cc 体检报告 · {time.strftime('%F %T')} · 耗时 {elapsed:.1f}s ═══")
        for mark, name, detail in results:
            print(f"{mark} {name}: {detail}")
        print(f"─── 红项 {len(red)} · 黄项 {len(yellow)} ───")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Generate a machine-readable operation report for aiduMEI.

The script is deliberately read-only. It queries the running API for health
and optionally extends that with local maintenance state when credentials are
available. It never prints raw credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ducky.version import SERVICE_VERSION  # noqa: E402
from ducky.utils import api_auth_headers  # noqa: E402

SCHEMA_VERSION = 1

# f0.3（A5）：24h LLM 降级率一级指标。失败 + 闸门超时占全部调用的比例达到阈值、
# 且样本够（≥ _LLM_DEGRADED_MIN_CALLS）才告警 —— 一天三五次调用里坏一次不该把
# 整份报告染成 warning（告警疲劳和白护栏一样会让人不再看这一栏）。
_LLM_DEGRADED_WARN_ENV = "AIDUMEI_LLM_DEGRADED_WARN_RATIO"
_LLM_DEGRADED_DEFAULT_RATIO = 0.05
_LLM_DEGRADED_MIN_CALLS = 20
_LLM_WINDOW_HOURS = 24
# update_crontab.sh 里 TASKS=( … ) 的一行一个任务（与 acceptance_check.sh 同一口径）
_TASK_ROW_RE = re.compile(r'^\s+"[a-z0-9_]+\|', re.M)


def _base_url() -> str:
    return (os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767")).rstrip("/")


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _git_describe() -> dict[str, Any]:
    """这台机器是哪个发布点、脏了多少（v20.3.2 正式版 · 用户审计 A）。

    上一版只报 `git_commit`。生产上它自报了一个**不在任何 tag 血脉上**的 hash ——
    脱敏重写历史后生产 `.git` 停在旧血脉，文件内容却与新 tag 逐字节一致。
    用户拿那个 hash 回小仓对不上任何东西。**用户要的不是 hash，是「我这台机器是
    哪个发布点，脏了多少」。** 所以：
      · `describe`：`git describe --tags --always --dirty`
      · `exact_tag`：恰好在某个 tag 上（`--exact-match`），否则 None
      · `dirty`：工作树有未提交改动
      · `anchored`：exact_tag 且不 dirty —— 只有它为 True，这份报告才配替机器背书
    """
    root = Path(__file__).resolve().parents[1]
    out: dict[str, Any] = {"describe": None, "exact_tag": None, "dirty": None, "anchored": False}
    def _run(args: list[str]) -> str | None:
        try:
            r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=3)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None
    out["describe"] = _run(["describe", "--tags", "--always", "--dirty"])
    out["exact_tag"] = _run(["describe", "--tags", "--exact-match"])
    porcelain = _run(["status", "--porcelain", "--untracked-files=no"])
    out["dirty"] = None if porcelain is None else bool(porcelain.strip())
    out["anchored"] = bool(out["exact_tag"]) and out["dirty"] is False
    return out


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _crontab_task_count() -> int | None:
    """意图数（--list 数的是脚本打算装什么，不是系统装了什么）。"""
    try:
        result = subprocess.run(
            [str(Path(__file__).resolve().parent / "update_crontab.sh"), "--list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
        return len(data.get("tasks", []))
    except Exception:
        return None


def _crontab_install_status() -> dict[str, Any] | None:
    """Read the real crontab and verify each task's name, schedule and command."""
    try:
        result = subprocess.run(
            [str(Path(__file__).resolve().parent / "update_crontab.sh"), "--installed"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
        return data
    except Exception:
        return None


def _manifest_task_count() -> int | None:
    """update_crontab.sh 里 TASKS 数组**声明**的任务数（清单的静态真相）。

    只在 `--list` 与 `--installed` 都拿不到数时兜底用 —— 它读的是同一份清单，
    所以任务增减时它跟着变，不会像写死的字面量那样漂移。
    """
    try:
        text = (Path(__file__).resolve().parent / "update_crontab.sh").read_text(encoding="utf-8")
    except OSError:
        return None
    if "TASKS=(" not in text:
        return None
    block = text.split("TASKS=(", 1)[1].split("\n)", 1)[0]
    return len(_TASK_ROW_RE.findall(block)) or None


def _required_task_count(maintenance: dict[str, Any]) -> int | None:
    """「应装几条」：意图数（--list）→ 核验器的清单数（--installed 的 expected）
    → TASKS 声明数。三者都读同一份清单；都拿不到就是 None（判不了，按未核验处理）。

    f0.3（A5）：此前兜底是字面量 8 —— v21.2.0 任务数 8→9 之后，「--list 失败 +
    装了 8 条」会被这个 8 判成装齐，正是它自己注释里说的那种数字漂移假绿灯。
    """
    for key in ("crontab_task_count", "crontab_expected_count"):
        value = maintenance.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return _manifest_task_count()


def _maintenance_block() -> dict[str, Any]:
    """维护块对公开/鉴权两形态都填（v20.3.1 · 外审 hy4 F-02 / 用户审计 🔴-5）。

    这块数据全部来自本地文件系统与 crontab，不依赖鉴权 —— 无凭据部署此前
    因缺 maintenance 键恒 exit 2，一个完全健康的实例永远报「警告」，
    假警报和假绿灯是同一种病：它替你签了字，但字没有信息量。
    同时缓存子进程结果——此前同一表达式调两次，一次报告 fork 三个
    update_crontab.sh。"""
    cron = _crontab_install_status()
    return {
        "crontab_task_count": _crontab_task_count(),
        "crontab_expected_count": cron.get("expected") if cron else None,
        "crontab_installed_count": cron.get("installed") if cron else None,
        "crontab_verified": bool(cron and cron.get("ok")),
        "crontab_tasks": cron.get("tasks", {}) if cron else {},
        "latest_backup": _latest_backup(Path(os.environ.get("AIDUMEM_BACKUP_ROOT", "backups"))),
    }


def _llm_leg_downshifts(hours: int, now: float | None = None) -> int | None:
    """窗口内 LLM 腿的降挡次数（gear.py 记在 memory_events 账本里）。

    降挡之后到恢复之前，写入一律跳过蒸馏（skipped_llm_gear_open）—— 这些「没调
    LLM」的写入不会出现在调用结局账本里，降挡次数就是它们的旁证。读不到记 None。
    """
    cutoff = datetime.fromtimestamp((now if now is not None else time.time()) - hours * 3600,
                                    tz=timezone.utc).isoformat()
    try:
        from ducky.utils import get_facts_conn
        conn = get_facts_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_events WHERE actor = 'gear_shifter' "
                "AND target_id = 'llm_leg' AND action = 'downshift' AND timestamp >= ?",
                (cutoff,)).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0
    except (ImportError, sqlite3.Error):
        return None


def _llm_degradation_block(now: float | None = None) -> dict[str, Any]:
    """24h LLM 降级率：(失败 + 闸门超时) / 全部调用，来自 ducky.llm_client 的结局账本
    （call_llm 与 mem0 抽取两条通道每次调用都记一笔）。"""
    from ducky.env_config import float_env

    threshold = float_env(_LLM_DEGRADED_WARN_ENV, _LLM_DEGRADED_DEFAULT_RATIO,
                          minimum=0.0, maximum=1.0)
    block: dict[str, Any] = {"window_hours": _LLM_WINDOW_HOURS, "warn_ratio": threshold,
                             "min_calls": _LLM_DEGRADED_MIN_CALLS}
    try:
        from ducky.llm_client import llm_outcome_window
        window = llm_outcome_window(_LLM_WINDOW_HOURS, now=now)
    except (ImportError, OSError, ValueError, TypeError) as exc:
        block.update({"error": f"{type(exc).__name__}: {exc}"[:160], "warning": None})
        return block
    degraded = int(window["failed"]) + int(window["gate_timeout"])
    total = int(window["total"])
    ratio = round(degraded / total, 4) if total else None
    enough = total >= _LLM_DEGRADED_MIN_CALLS
    block.update({
        "calls": total, "ok": window["ok"], "failed": window["failed"],
        "gate_timeouts": window["gate_timeout"], "degraded": degraded, "ratio": ratio,
        "by_origin": window["by_origin"], "ledger_present": window["ledger_present"],
        "llm_leg_downshifts": _llm_leg_downshifts(_LLM_WINDOW_HOURS, now),
        "warning": bool(enough and ratio is not None and ratio >= threshold),
        "note": None if enough else f"样本不足（{total} < {_LLM_DEGRADED_MIN_CALLS} 次调用），不判降级",
    })
    return block


def _latest_backup(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {"path": None, "age_hours": None, "verified": None}
    candidates = [p for p in root.iterdir() if p.is_dir()]
    if not candidates:
        return {"path": None, "age_hours": None, "verified": None}
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    verified = (latest / ".backup_verified").exists()
    age_hours = round((time.time() - latest.stat().st_mtime) / 3600, 2)
    return {"path": latest.name, "age_hours": age_hours, "verified": verified}


def _safe_next_actions(health: dict[str, Any], maintenance: dict[str, Any] | None = None,
                       llm: dict[str, Any] | None = None) -> list[str]:
    actions: list[str] = []
    if health.get("health_status") != "ok":
        actions.append("Inspect the authenticated /health response and resolve degraded components.")
    _degraded = health.get("degraded") or []
    if _degraded:
        actions.append("Resolve degraded components before relying on semantic recall.")
    # 泛泛一句「有组件降级」对 ingest_liveness 是误导：问题不在召回质量，
    # 而在**新记忆压根没写进来**，且这件事不会自愈。建议必须点名要做什么。
    if "ingest_liveness" in _degraded or "epistemic_session_coverage" in _degraded:
        actions.append(
            "WRITE WIRE LOOKS BROKEN: the host reads memories but never writes any. "
            "Hook the write side (Hermes post_llm_call / Claude Code Stop) as described "
            "in docs/AGENT_INTEGRATION.md, then verify with "
            "python3 scripts/check_ingest_wiring.py (exit code must be 0)."
        )
    if health.get("warming_up"):
        actions.append("Wait for warm-up components or trigger a normal request before deep diagnosis.")
    if llm and llm.get("warning"):
        actions.append(
            f"LLM DEGRADED: {llm.get('degraded')}/{llm.get('calls')} LLM calls in the last "
            f"{llm.get('window_hours')}h failed or timed out at the concurrency gate "
            f"(ratio {llm.get('ratio')} >= {llm.get('warn_ratio')}). Check upstream quota/"
            "concurrency and AIDUMEI_LLM_MAX_CONCURRENCY; writes fall back to direct storage "
            "without distillation while this lasts.")
    maintenance = maintenance or _maintenance_block()
    installed = maintenance.get("crontab_installed_count")
    effective = installed
    _required = _required_task_count(maintenance)
    if (effective is None or _required is None or effective < _required
            or maintenance.get("crontab_verified") is False):
        actions.append(
            "Run bash scripts/update_crontab.sh install to install maintenance jobs "
            "(maintenance is NOT fully installed; this is based on the real crontab)."
        )
    backup = maintenance.get("latest_backup") or {}
    if not backup.get("verified"):
        actions.append("Create and verify a backup with scripts/backup_gate.sh.")
    if not actions:
        actions.append("System is healthy; run scripts/e2e_smoke.py after the next deployment or restore.")
    return actions


def _engine_mode_of(health: dict[str, Any]) -> Any:
    """engine_mode 真身在 probes.engine_mode_policy（v20.3.1 · 用户审计 🟡-7）。

    此前从 /health 顶层读，顶层根本没这个键 → 恒 null。一行 Prompt 第 11 步
    让 agent 汇报「挡位」，拿到的永远是空。"""
    probes = health.get("probes") or {}
    policy = probes.get("engine_mode_policy") or {}
    return policy.get("configured") or policy.get("mode") or health.get("engine_mode")


def _public_report(health: dict[str, Any]) -> dict[str, Any]:
    maintenance = _maintenance_block()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "service_version": SERVICE_VERSION,
        "git_commit": _git_commit(),
        "git_describe": _git_describe(),
        "health_status": health.get("health_status"),
        "status": health.get("status"),
        "engine_mode": _engine_mode_of(health),
        "degraded": health.get("degraded", []),
        "warming_up": health.get("warming_up", []),
        "maintenance": maintenance,
        "next_actions": _safe_next_actions(health, maintenance),
    }


def _full_report(health: dict[str, Any]) -> dict[str, Any]:
    probes = health.get("probes") or {}
    report = _public_report(health)
    llm = _llm_degradation_block()
    warnings = list(health.get("warnings", []))
    if llm.get("warning"):
        warnings.append(
            f"24h LLM 降级率 {llm.get('ratio')} ≥ 阈值 {llm.get('warn_ratio')}"
            f"（{llm.get('degraded')}/{llm.get('calls')} 次调用失败或等不到并发空位）")
    report["llm_degradation_24h"] = llm
    report["next_actions"] = _safe_next_actions(health, report["maintenance"], llm)
    report.update({
        "capacity": {
            "facts_active_count": probes.get("facts_active_count"),
            "facts_watermark_effective": probes.get("facts_watermark_effective"),
            "wal_total_bytes": probes.get("wal_total_bytes"),
            "wal_alert_dbs": probes.get("wal_alert_dbs"),
            "process_rss_mb": probes.get("process_rss_mb"),
            "process_max_rss_mb": probes.get("process_max_rss_mb"),
        },
        "anomalies": {
            "warnings": warnings,
            "feature_failures": probes.get("feature_failures"),
            "feature_failures_by_name": probes.get("feature_failures_by_name"),
        },
        "health": health,
    })
    return report


def _exit_code(report: dict[str, Any]) -> int:
    if report.get("health_status") != "ok" or report.get("degraded"):
        return 3
    # v20.3.2 正式版（用户审计 A）：一台脱锚的机器（不在任何 tag 上 / 工作树脏）
    # 不许拿到 0。0 等于替它背书；2（warning）让读者去看 git_describe。
    gd = report.get("git_describe") or {}
    if gd.get("describe") is not None and not gd.get("anchored"):
        return 2
    if report.get("warming_up") or report.get("anomalies", {}).get("warnings"):
        return 2
    if (report.get("llm_degradation_24h") or {}).get("warning"):
        return 2
    maintenance = report.get("maintenance", {}) or {}
    installed = maintenance.get("crontab_installed_count")
    effective = installed
    # 门槛跟着清单走，不写字面量。v21.2.0 把任务数 8 → 9 时，原来的
    # `< 8` 会让「装了 8 条、少了写线哨兵」这种情况判成装齐——数字漂移
    # 造的假绿灯，和探针不在场是同一种病。f0.3：连兜底也不再是字面量 8，
    # 改读同一份清单（见 _required_task_count）；清单数都读不到 = 判不了 = warning。
    _required = _required_task_count(maintenance)
    if (effective is None or _required is None or effective < _required
            or maintenance.get("crontab_verified") is False):
        return 2
    if not (maintenance.get("latest_backup") or {}).get("verified"):
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a single JSON object")
    args = parser.parse_args()
    headers = {"Accept": "application/json"}
    headers.update(api_auth_headers())
    try:
        public = _get_json(f"{_base_url()}/health", {"Accept": "application/json"})
        full = _get_json(f"{_base_url()}/health", headers)
    except urllib.error.HTTPError as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "fail", "http_error": exc.code}))
        return 3
    except Exception as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "fail", "error": str(exc)[:200]}))
        return 3
    report = _full_report(full) if headers.get("Authorization") else _public_report(public)
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=True))
    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())

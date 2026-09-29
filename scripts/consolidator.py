#!/usr/bin/env python3
"""
aiduMEM Consolidator — 24h 后台合并器（HTTP 版）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v8.3.0 — 零记忆同化升级：
- Lane 感知衰减（identity/preference 铁律不衰减）
- 矛盾检测（同 Lane 内反义词碰撞）
- 每日生长指标（daily_metrics 表）
- 噩梦推演（5% 触发健康审计）

f0.3 — 让淘汰说真话（生产实况：日志连续 11 天写「通过 API 删除 0/N」，
cascade_delete 墓碑却每天都在长 —— /delete 自 v20.2.5-b 起只回
committed / not_found / partial / failed，旧判据 `status == "ok"` 恒为 False）：
- 淘汰档位 AIDUMEI_CONSOLIDATOR_EVICT = off | dry-run | apply，**默认 dry-run**
  （只列候选、一条不删）；非法值按 dry-run 处理并告警。环境变量优先，其次 .env
  （cron 不加载 .env，只认环境变量的开关到点就不生效）。
- apply 按 /delete 的真实状态逐条记账：committed→已删除，not_found→本来就不在
  （仅在确认 not_found 时清掉本地残留的显著性行，免得它每天被重新选中），
  207/partial→部分失败，500/HTTP 错误/网络错误→失败；有失败或部分失败打 WARNING。
- 删除按候选行自己的 (user_id, bank_id) 发出，不再一律打默认域。
- 每轮（含中止与异常）写 <DATA_DIR>/consolidator_last_run.json，
  /health 授权视图的 consolidator 探针读它。
- 日志每行只写一次：FileHandler 常驻；StreamHandler 只在 stderr 是终端时才挂 ——
  cron 的 `>> consolidator.log 2>&1` 与 FileHandler 写的是同一个文件。
"""

import http.client
import json
import logging
import os
import random
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ducky.utils import DEFAULT_USER_ID, LOG_DIR, env_or_env_file
from ducky.memory_salience import (decay_all, get_stats,
                                    resolve_conflict_salience, record_daily_metrics,
                                    audit_health_anomalies)
from ducky.salience import (conflict_penalty_mode_status, consolidator_last_run_path,
                            delete_salience, scan_conflicts)
from ducky.skill_crystallizer import detect_and_crystallize_patterns

# 🔴P0-1（v19.4.1）：凭据从 ducky.utils 统一取（环境变量 → .env 兜底）。
# cron 不会加载 .env，若各脚本各自读环境变量，门禁一开就会集体静默 401。
from ducky.utils import api_auth_headers as _auth_headers  # noqa: E402


logger = logging.getLogger("aiduMEM.consolidator")

API_BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")

# ── f0.3（A3）：日志只写一次 ────────────────────────────────────────────
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_HANDLER_TAG = "_aidumei_consolidator_handler"


def _stream_is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def configure_logging(stream=None, log_dir=None) -> list:
    """给根 logger 装本脚本的 handler，返回装上的 handler 列表。

    原来是 import 期的 basicConfig：FileHandler(consolidator.log) + StreamHandler(stderr)。
    cron 又把 stderr `>> consolidator.log 2>&1` 进**同一个文件**，于是每行写两遍
    （生产日志 46–78 MB/天）。现在 FileHandler 常驻；StreamHandler 只在 stream
    是终端时才挂 —— 人手跑能在屏幕上看见，cron 跑只写一份。
    只在脚本入口调用（import 本模块不再动全局日志配置）；重复调用先拆掉自己
    上次装的那几个，不叠加。
    """
    root = logging.getLogger()
    for old in list(root.handlers):
        if getattr(old, _HANDLER_TAG, False):
            root.removeHandler(old)
            old.close()
    target_dir = log_dir or LOG_DIR
    handlers: list = [logging.FileHandler(os.path.join(target_dir, "consolidator.log"),
                                          encoding="utf-8")]
    stream = sys.stderr if stream is None else stream
    if _stream_is_tty(stream):
        handlers.append(logging.StreamHandler(stream))
    formatter = logging.Formatter(_LOG_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    return handlers


# ── f0.3（A1）：淘汰档位 ────────────────────────────────────────────────
_EVICT_ENV = "AIDUMEI_CONSOLIDATOR_EVICT"
EVICT_MODES = ("off", "dry-run", "apply")
DEFAULT_EVICT_MODE = "dry-run"
# /delete 的真实状态 → 本脚本的记账口径
DELETE_OUTCOMES = ("deleted", "already_gone", "partial", "failed")
_DRY_RUN_LOG_LIMIT = 200         # dry-run 日志逐条列出的上限（完整名单进运行摘要）
_SUMMARY_LIST_LIMIT = 5000       # 运行摘要里候选名单的上限
_FAILURE_LIST_LIMIT = 50
SUMMARY_SCHEMA_VERSION = 1


def evict_mode_status() -> dict:
    """淘汰档位的生效值 + 原始值 + 配置错误。非法值回落 dry-run（一条不删）并告警。"""
    raw = env_or_env_file(_EVICT_ENV, "")
    value = raw.strip().lower()
    if not value:
        return {"mode": DEFAULT_EVICT_MODE, "raw": None, "error": None}
    if value in EVICT_MODES:
        return {"mode": value, "raw": raw, "error": None}
    error = (f"{_EVICT_ENV}={raw!r} 不是 off|dry-run|apply 之一，"
             f"已按默认 {DEFAULT_EVICT_MODE} 处理（只列候选，一条不删）")
    logger.warning("⚠️ %s", error)
    return {"mode": DEFAULT_EVICT_MODE, "raw": raw, "error": error}


def _with_file_lock(fn):
    """与 api_server 后台小时循环共用 consolidator.lock，防双跑。"""
    from ducky.utils import CONSOLIDATOR_LOCK
    lock_path = CONSOLIDATOR_LOCK
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    if sys.platform == "win32":
        import msvcrt
        def _lock(f):
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        def _unlock(f):
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        def _lock(f):
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        def _unlock(f):
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    with open(lock_path, "w") as lf:
        try:
            _lock(lf)
        except BlockingIOError:
            logger.info("⏭️ consolidator 跳过：另一实例持锁")
            return None
        try:
            return fn()
        finally:
            _unlock(lf)


def _api_get(endpoint: str) -> dict:
    url = f"{API_BASE}{endpoint}"
    try:
        req = urllib.request.Request(url, headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # 401/403 必须显式记日志：静默失败会让「门禁开了但脚本没带钥匙」
        # 这种配置错误长期潜伏。
        logger.error(f"  HTTP {e.code} → GET {endpoint}"
                     f"{'（未携带 AIDUMEM_API_TOKEN？）' if e.code in (401, 403) else ''}")
        return {}
    except Exception as e:
        logger.debug(f"  API GET 失败 {endpoint}: {e}")
        return {}

def _api_post(endpoint: str, data: dict) -> dict:
    url = f"{API_BASE}{endpoint}"
    body = json.dumps(data).encode()
    headers = {"Content-Type": "application/json", **_auth_headers()}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.error(f"  HTTP {e.code} → {endpoint}"
                     f"{'（未携带 AIDUMEM_API_TOKEN？）' if e.code in (401, 403) else ''}")
        return {}
    except Exception as e:
        # 原实现这里是 HTTPError 分支 return 之后的死代码，异常被完全吞掉。
        logger.error(f"  API 调用失败 {endpoint}: {e}")
        return {}


def _post_json_with_status(endpoint: str, data: dict, timeout: float = 30) -> tuple:
    """POST 并把 **HTTP 状态码** 与响应体一起带回：(code | None, body, error | None)。

    `_api_post` 在 HTTPError 时直接回 {}，读不到 207/500 的响应体 —— 删除记账
    必须分得清「部分失败」与「失败」，所以这里单开一条保留状态码的通道。
    """
    url = f"{API_BASE}{endpoint}"
    body = json.dumps(data).encode()
    headers = {"Content-Type": "application/json", **_auth_headers()}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code, raw = resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            raw = e.read()
        except (OSError, http.client.HTTPException):
            raw = b""
        if code in (401, 403):
            logger.error(f"  HTTP {code} → {endpoint}（未携带 AIDUMEM_API_TOKEN？）")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        return None, {}, f"{type(e).__name__}: {e}"[:200]
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        parsed = {}
    return code, (parsed if isinstance(parsed, dict) else {}), None


def classify_delete_response(code, body: dict, error=None) -> str:
    """/delete 的 (HTTP 状态码, 响应体) → 记账口径（DELETE_OUTCOMES 之一）。

    ducky/hot/crud.py 的契约：committed / not_found 走 200，partial 走 207，
    failed 走 500。认不出的状态一律记失败 —— 确认不了就不许算成删掉了。
    """
    if error is not None or code is None:
        return "failed"
    status = str((body or {}).get("status") or "")
    if code == 207 or status == "partial":
        return "partial"
    if code == 200 and status == "committed":
        return "deleted"
    if code == 200 and status == "not_found":
        return "already_gone"
    return "failed"


def _sidecar_only(body: dict) -> bool:
    """committed 但任何**内容层**都没命中：只清掉了显著性/检索反馈这类侧车行。

    历史教训（v19.4.1）：「删除成功 25/25」全是幽灵 id 的空转。这类 committed
    照实计入「已删除」，但单列出来，别让它们冒充真删掉了一条记忆。
    """
    d = (body or {}).get("details") or {}
    if not isinstance(d, dict):
        return False
    content_hit = (bool(d.get("mem0_vector")) or bool(d.get("local_vector_deleted"))
                   or bool(d.get("workspace_evicted"))
                   or any(int(d.get(k) or 0) > 0 for k in ("fts_rows", "facts", "verbatim")))
    return not content_hit


def _delete_via_api(memory_id: str, user_id: str = DEFAULT_USER_ID,
                    bank_id: str = "default") -> dict:
    code, body, error = _post_json_with_status(
        "/delete", {"memory_id": memory_id, "user_id": user_id, "bank_id": bank_id})
    outcome = classify_delete_response(code, body, error)
    return {
        "outcome": outcome,
        "http_status": code,
        "status": body.get("status"),
        "sidecar_only": outcome == "deleted" and _sidecar_only(body),
        "failed_layers": body.get("failed_layers") if outcome in ("partial", "failed") else None,
        "error": error,
    }


def _delete_scope(user_id: str, bank_id: str) -> tuple:
    """salience 行上的作用域 → /delete 的作用域。

    存量行（与未传作用域的写入）盖的是字面量 'default'，它就是默认身份的域
    （与 conflict._canon_uid 同一口径）；其余原样透传。f0.3 之前所有删除一律打
    DEFAULT_USER_ID 的默认库，具名域的候选因此只会被删掉显著性行、记忆本体不动。
    """
    user = DEFAULT_USER_ID if user_id in ("", "default") else user_id
    return user, (bank_id or "default")


def _candidate_list(result: dict) -> list:
    """decay_all 的淘汰候选 → 带作用域与理由的名单（不含正文）。"""
    from ducky.salience.core import get_batch_salience_records

    ids = [str(m) for m in (result.get("evicted") or [])]
    details = {str(d.get("memory_id")): d for d in (result.get("evicted_details") or [])}
    records: dict = {}
    for start in range(0, len(ids), 500):
        records.update(get_batch_salience_records(ids[start:start + 500]) or {})
    out = []
    for mid in ids:
        rec, det = records.get(mid) or {}, details.get(mid) or {}
        out.append({
            "memory_id": mid,
            "user_id": str(rec.get("user_id") or "default"),
            "bank_id": str(rec.get("bank_id") or "default"),
            "lane": det.get("lane") or rec.get("lane"),
            "salience": det.get("salience"),
            "idle_days": det.get("idle_days"),
        })
    return out


def _reason(c: dict) -> str:
    sal, idle = c.get("salience"), c.get("idle_days")
    sal_s = f"{sal:.3f}" if isinstance(sal, (int, float)) else "?"
    idle_s = f"{idle:.0f}" if isinstance(idle, (int, float)) else "?"
    return f"salience={sal_s} 闲置{idle_s}天 lane={c.get('lane')} scope={c['user_id']}/{c['bank_id']}"


def _log_dry_run(candidates: list) -> None:
    logger.info(f"🔍 淘汰 [dry-run]：{len(candidates)} 条候选，本轮一条不删"
                f"（{_EVICT_ENV}=apply 才会真删）")
    for c in candidates[:_DRY_RUN_LOG_LIMIT]:
        logger.info(f"   候选 {c['memory_id']} | {_reason(c)}")
    if len(candidates) > _DRY_RUN_LOG_LIMIT:
        logger.info(f"   …… 另有 {len(candidates) - _DRY_RUN_LOG_LIMIT} 条未逐条列出"
                    f"（完整名单见 {consolidator_last_run_path()}）")


def _new_deletion_counts() -> dict:
    counts = {k: 0 for k in DELETE_OUTCOMES}
    counts.update({"attempted": 0, "deleted_sidecar_only": 0, "stale_salience_rows_removed": 0})
    return counts


def _apply_evictions(candidates: list) -> tuple:
    """逐条调 /delete，按真实状态记账。返回 (counts, failures)。"""
    counts, failures = _new_deletion_counts(), []
    for c in candidates:
        user, bank = _delete_scope(c["user_id"], c["bank_id"])
        res = _delete_via_api(c["memory_id"], user, bank)
        counts["attempted"] += 1
        counts[res["outcome"]] += 1
        if res["sidecar_only"]:
            counts["deleted_sidecar_only"] += 1
        if res["outcome"] == "already_gone":
            # 只在服务端**确认** not_found 时清本地残留行：否则它每天都会被
            # decay_all 重新选中、再被 /delete 回一次 not_found，永不收敛。
            counts["stale_salience_rows_removed"] += delete_salience([c["memory_id"]])
        elif res["outcome"] in ("partial", "failed") and len(failures) < _FAILURE_LIST_LIMIT:
            failures.append({"memory_id": c["memory_id"], "user_id": user, "bank_id": bank,
                             **{k: res[k] for k in ("outcome", "http_status", "status",
                                                    "failed_layers", "error")}})
    return counts, failures


def _log_apply(n: int, counts: dict, mismatch: bool) -> None:
    msg = (f"🗑️ 淘汰 [apply]：候选 {n} 条 → 已删除 {counts['deleted']}"
           f"（其中只清掉侧车行 {counts['deleted_sidecar_only']}）"
           f" / 本来就不在 {counts['already_gone']}"
           f"（清理本地残留显著性行 {counts['stale_salience_rows_removed']}）"
           f" / 部分失败 {counts['partial']} / 失败 {counts['failed']}")
    if counts["partial"] or counts["failed"]:
        logger.warning(msg)
    else:
        logger.info(msg)
    if mismatch:
        logger.warning(f"⚠️ 淘汰对账不平：候选 {n} ≠ 已删除 {counts['deleted']}"
                       f" + 本来就不在 {counts['already_gone']}，差额在部分失败/失败里，"
                       "明细见运行摘要 failures")


def _eviction_step(mode: str, candidates: list, summary: dict) -> int:
    """按档位处置淘汰候选，结果写进 summary；返回本轮真删掉的条数。"""
    summary["candidate_list"] = candidates[:_SUMMARY_LIST_LIMIT]
    summary["candidate_list_truncated"] = len(candidates) > _SUMMARY_LIST_LIMIT
    summary["deletion"] = _new_deletion_counts()
    summary["mismatch"] = None
    summary["failures"] = []
    if mode == "off":
        summary["candidate_list"] = []
        logger.info(f"⏸️ 淘汰 [off]：{len(candidates)} 条候选按配置不处置、不列名")
        return 0
    if mode == "dry-run":
        _log_dry_run(candidates)
        return 0
    counts, failures = _apply_evictions(candidates)
    mismatch = len(candidates) != counts["deleted"] + counts["already_gone"]
    summary.update({"deletion": counts, "mismatch": mismatch, "failures": failures})
    _log_apply(len(candidates), counts, mismatch)
    return counts["deleted"]


def _conflict_step() -> dict:
    """矛盾检测：按 AIDUMEI_CONFLICT_PENALTY_MODE 扫描 / 处置，返回统计。"""
    status = conflict_penalty_mode_status()
    mode = status["mode"]
    out = {"mode": mode, "mode_error": status["error"], "skipped": mode == "off",
           "pairs_found": 0, "pairs_compared": 0, "pairs_total": 0, "pairs_applied": 0,
           "memories_penalized": 0, "truncated": False, "truncated_groups": 0,
           "max_pairs_per_lane": None, "examples": [], "error": None}
    if mode == "off":
        logger.info("⏸️ 矛盾检测 [off]：按配置跳过扫描")
        return out
    try:
        scan = scan_conflicts()
        conflicts = scan.pop("conflicts")
        penalized = resolve_conflict_salience(conflicts, mode=mode)
    except Exception as e:
        logger.warning(f"矛盾检测跳过: {e}")
        out["error"] = f"{type(e).__name__}: {e}"[:200]
        return out
    out.update({k: scan[k] for k in ("pairs_found", "pairs_compared", "pairs_total",
                                     "truncated", "truncated_groups", "max_pairs_per_lane")})
    out["pairs_applied"] = len(conflicts) if mode == "apply" else 0
    out["memories_penalized"] = penalized
    out["examples"] = [{k: c.get(k) for k in ("kind", "word_pair", "similarity", "lane",
                                               "memory_a", "memory_b")}
                       for c in conflicts[:20]]
    if not conflicts:
        logger.info(f"✅ 矛盾检测 [{mode}]：无冲突（比对 {scan['pairs_compared']} 对）")
    if scan["truncated"]:
        logger.warning(f"⚠️ 矛盾检测截断：{scan['truncated_groups']} 个组超过每组"
                       f" {scan['max_pairs_per_lane']} 对的比对上限，只比了"
                       f" {scan['pairs_compared']}/{scan['pairs_total']} 对")
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _new_summary(started: float) -> dict:
    return {"schema_version": SUMMARY_SCHEMA_VERSION, "started_at": _iso(started),
            "status": "running", "abort_reason": None, "error": None,
            "mode": None, "mode_raw": None, "mode_error": None, "candidates": None,
            "deletion": _new_deletion_counts(), "mismatch": None, "failures": [],
            "candidate_list": [], "candidate_list_truncated": False,
            "conflicts": None, "salience": None, "pid": os.getpid()}


def write_summary(summary: dict, path: str = "") -> bool:
    """原子写运行摘要（先写临时文件再 rename，读的一方永远看不到半截 JSON）。"""
    target = path or consolidator_last_run_path()
    tmp = f"{target}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        os.replace(tmp, target)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.error(f"❌ 运行摘要写入失败 {target}: {e}")
        return False


def _get_all_via_api(user_id: str, limit: int = 5000) -> list:
    result = _api_post("/search", {"query": "aiduMEM 记忆 升级 配置",
                                   "user_id": user_id, "caller_user_id": user_id,
                                   "limit": limit})
    return result.get("results", [])


def _check_api_alive() -> bool:
    return bool(_api_get("/health"))


def run_consolidation():
    """主合并流程（衰减→矛盾→淘汰→毕业→指标→噩梦），返回本轮运行摘要。

    f0.3：无论正常结束、中止（api_server 不可达）还是中途抛异常，都会写
    consolidator_last_run.json —— 「这一轮到底做了什么」不许只活在日志里。
    """
    def _body():
        start_ts = time.time()
        summary = _new_summary(start_ts)
        try:
            _consolidate(summary)
        except Exception as e:
            summary["status"] = "error"
            summary["error"] = f"{type(e).__name__}: {e}"[:300]
            logger.exception("❌ 合并中途异常，已写运行摘要后退出")
            raise
        finally:
            finished = time.time()
            summary["finished_at"] = _iso(finished)
            summary["timestamp"] = round(finished, 3)
            summary["duration_s"] = round(finished - start_ts, 3)
            write_summary(summary)
        return summary

    # B 档：文件锁包一层，与 api 后台小时循环互斥
    return _with_file_lock(_body)


def _consolidate(summary: dict) -> None:
    logger.info("🧹 f0.3 开始 24h 后台合并...")
    start_ts = time.time()
    mode_info = evict_mode_status()
    summary.update({"mode": mode_info["mode"], "mode_raw": mode_info["raw"],
                    "mode_error": mode_info["error"]})
    mode = mode_info["mode"]

    # ── 前置检查：api_server 存活？ ──
    if not _check_api_alive():
        logger.error("❌ api_server 不可达，中止合并")
        summary.update({"status": "aborted", "abort_reason": "api_unreachable"})
        return

    # ── Step 1: Salience 衰减（v8.3.0: Lane 感知乘系数）──
    stats_before = get_stats()
    result = decay_all()
    stats_after = get_stats()

    candidates = _candidate_list(result)
    decayed = result["updated"]
    summary["candidates"] = len(candidates)
    summary["salience"] = {
        "tracked_before": stats_before["total_tracked"],
        "tracked_after": stats_after["total_tracked"],
        "avg_before": stats_before["avg_salience"],
        "avg_after": stats_after["avg_salience"],
        "decayed": decayed,
    }
    logger.info(f"📊 Salience: {stats_before['total_tracked']}条 → {stats_after['total_tracked']}条 "
                f"(淘汰候选{len(candidates)}, 均值{stats_before['avg_salience']:.3f}→{stats_after['avg_salience']:.3f})")

    # ── Step 2: 矛盾检测（f0.3：判据重写 + 处置档位，默认只告警）──
    summary["conflicts"] = _conflict_step()

    # ── Step 3: 淘汰（f0.3：档位 + 按 /delete 真实状态记账）──
    deleted = _eviction_step(mode, candidates, summary)

    #         if grad_result.get("graduated_groups", 0) > 0:
    #             logger.info(f"🎓 Instinct 毕业: {grad_result['graduated_groups']}组 → "
    #                        f"{len(grad_result.get('new_skills', []))}条 skill, "
    #                        f"删除{grad_result.get('deleted', 0)}条源记忆")
    #     else:
    #         logger.info("🎓 Instinct graduation 跳过（无记忆数据）")
    # except Exception as ge:
    #     logger.warning(f"⚠️ Instinct graduation 跳过: {ge}")
    logger.info("🎓 Instinct graduation 自动毕业已被手动禁用")

    # ── Step 5: v8.3.0 每日生长指标 ──
    # f0.3：evicted 记**真删掉的条数**。原来记的是候选数 —— dry-run 下一条没删，
    # daily_metrics 却会写「淘汰 N 条」，指标先于日志说了谎。
    try:
        metrics = record_daily_metrics(decayed=decayed, evicted=deleted)
        logger.info(f"📈 每日指标记录完成: {json.dumps(metrics, ensure_ascii=False)}")
    except Exception as e:
        logger.warning(f"每日指标记录失败: {e}")

    # ── Step 5b: v9.2 教训自动闭环验证 (Aethelgard 专属) ──
    try:
        from ducky.memory_salience import verify_lessons_closed
        verify_res = verify_lessons_closed()
        logger.info(f"🎓 教训闭环验证完成: 已处理 {verify_res.get('processed', 0)} 条, "
                    f"报警强拉 {verify_res.get('boosted', 0)} 条, 归档 {verify_res.get('closed', 0)} 条")
    except Exception as e:
        logger.warning(f"教训闭环验证失败: {e}")

    # ── Step 5c: 🐙 v16.0 Opus Octopod (opus八爪鱼) 技能结晶感知 ──
    try:
        crystals = detect_and_crystallize_patterns()
        logger.info(f"🐙 [Opus Octopod] 技能结晶感知完成: 生成 {len(crystals)} 个候选项")
    except Exception as e:
        logger.warning(f"🐙 技能结晶感知失败: {e}")

    # ── Step 6: v8.3.0 噩梦推演（5% 概率）──
    if random.random() < 0.05:
        try:
            nightmare = audit_health_anomalies()
            if nightmare["triggered"]:
                logger.warning(f"👻 噩梦推演: {nightmare['alerts']}")
        except Exception as e:
            logger.warning(f"噩梦推演失败: {e}")
    else:
        logger.debug("💤 噩梦未触发（95% 跳过）")

    summary["status"] = "ok"
    elapsed = time.time() - start_ts
    logger.info(f"✅ 合并完成 ({elapsed:.1f}s)")


if __name__ == "__main__":
    configure_logging()
    run_consolidation()

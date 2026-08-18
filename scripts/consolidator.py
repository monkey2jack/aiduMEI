#!/usr/bin/env python3
"""
aiduMEM Consolidator — 24h 后台合并器（HTTP 版）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v8.3.0 — 零记忆同化升级：
- Lane 感知衰减（identity/preference 铁律不衰减）
- 矛盾检测（同 Lane 内反义词碰撞）
- 每日生长指标（daily_metrics 表）
- 噩梦推演（5% 触发健康审计）
"""

import json
import logging
import os
import random
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ducky.utils import DEFAULT_USER_ID, LOG_DIR
from ducky.memory_salience import (decay_all, get_stats, detect_conflicts,
                                    resolve_conflict_salience, record_daily_metrics,
                                    audit_health_anomalies)
from ducky.skill_crystallizer import detect_and_crystallize_patterns

# 🔴P0-1（v19.4.1）：凭据从 ducky.utils 统一取（环境变量 → .env 兜底）。
# cron 不会加载 .env，若各脚本各自读环境变量，门禁一开就会集体静默 401。
from ducky.utils import api_auth_headers as _auth_headers  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "consolidator.log")),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("aiduMEM.consolidator")

API_BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")


def _with_file_lock(fn):
    """与 api_server 后台小时循环共用 consolidator.lock，防双跑。"""
    import fcntl
    from ducky.utils import CONSOLIDATOR_LOCK
    lock_path = CONSOLIDATOR_LOCK
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("⏭️ consolidator 跳过：另一实例持锁")
            return None
        try:
            return fn()
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


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


def _delete_via_api(memory_id: str) -> bool:
    result = _api_post("/delete", {"memory_id": memory_id, "user_id": DEFAULT_USER_ID})
    return result.get("status") == "ok"


def _get_all_via_api(user_id: str, limit: int = 5000) -> list:
    result = _api_post("/search", {"query": "aiduMEM 记忆 升级 配置", "user_id": user_id, "limit": limit})
    return result.get("results", [])


def _check_api_alive() -> bool:
    return bool(_api_get("/health"))


def run_consolidation():
    """主合并流程（v8.3.0 六步：衰减→矛盾→踢出→毕业→指标→噩梦）"""
    def _body():
        logger.info("🧹 v8.3.0 开始 24h 后台合并...")
        start_ts = time.time()

        # ── 前置检查：api_server 存活？ ──
        if not _check_api_alive():
            logger.error("❌ api_server 不可达，中止合并")
            return

        # ── Step 1: Salience 衰减（v8.3.0: Lane 感知乘系数）──
        stats_before = get_stats()
        result = decay_all()
        stats_after = get_stats()

        evicted = result["evicted"]
        decayed = result["updated"]
        logger.info(f"📊 Salience: {stats_before['total_tracked']}条 → {stats_after['total_tracked']}条 "
                    f"(踢出{len(evicted)}, 均值{stats_before['avg_salience']:.3f}→{stats_after['avg_salience']:.3f})")

        # ── Step 2: v8.3.0 矛盾检测 ──
        try:
            conflicts = detect_conflicts()
            if conflicts:
                resolved = resolve_conflict_salience(conflicts)
                logger.warning(f"⚔️ 矛盾检测: 发现 {len(conflicts)} 组矛盾, 降低 {resolved} 条记忆显著性")
            else:
                logger.info("✅ 矛盾检测: 无冲突")
        except Exception as e:
            logger.warning(f"矛盾检测跳过: {e}")

        # ── Step 3: 通过 API 删除被踢出的记忆 ──
        if evicted:
            deleted_ok = 0
            for mid in evicted:
                if _delete_via_api(mid):
                    deleted_ok += 1
                else:
                    logger.debug(f"删除失败 {mid[:16]}")
            logger.info(f"🗑️ 通过 API 删除 {deleted_ok}/{len(evicted)} 条低显著性记忆")

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
        try:
            metrics = record_daily_metrics(decayed=decayed, evicted=len(evicted))
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

        elapsed = time.time() - start_ts
        logger.info(f"✅ 合并完成 ({elapsed:.1f}s)")

    # B 档：文件锁包一层，与 api 后台小时循环互斥
    _with_file_lock(_body)


if __name__ == "__main__":
    run_consolidation()

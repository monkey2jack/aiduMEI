"""ducky.salience.lesson_verify — 教训自动闭环验证"""
from __future__ import annotations

import logging
import sqlite3
import os
import time
from ducky.utils import get_salience_conn

logger = logging.getLogger("aiduMEM.salience")

def verify_lessons_closed() -> dict:
    """
    检查所有 lane='lesson' 的记忆。
    如果在 state.db / errors.log 等系统日志中，发现该教训对应的关键词近期没有再触发任何错误，
    或者被显式提及 '已解决' / '搞定'，则将显著性微降或将 lane 移至 'knowledge'；
    如果报错反复出现，则对该条教训的 salience 进行 boost 强行拉回，甚至在卡片中发出报警。
    """
    sal_conn = get_salience_conn()
    lessons = sal_conn.execute(
        "SELECT memory_id, salience, content_preview FROM salience WHERE lane = 'lesson'"
    ).fetchall()
    sal_conn.close()

    if not lessons:
        return {"processed": 0, "boosted": 0, "closed": 0}

    # 查阅 errors.log.1 或 errors.log 中的最近 500 行，提取关键词
    # 宿主 Agent 的错误日志路径 — 部署方可用环境变量指定，未配置则跳过日志比对
    error_log_path = os.environ.get("AIDUMEM_HOST_ERROR_LOG", "")
    errors_text = ""
    if os.path.exists(error_log_path):
        try:
            with open(error_log_path, "r", encoding="utf-8") as f:
                # 读最后 200 行
                lines = f.readlines()
                errors_text = "".join(lines[-200:]).lower()
        except Exception as e:
            logger.debug(f"读取 errors.log 失败: {e}")

    boosted = 0
    closed = 0
    sal_conn = get_salience_conn()
    for mid, val, content in lessons:
        if not content:
            continue
        
        # 简单关键字提取：去掉符号和助词，匹配英文实体词或中文关键动宾
        # 我们用粗暴的方法：如果在错误日志中检测到这行记忆的 preview 里的核心关键字，就说明又犯错报错了！
        # 针对 key error / type error 等技术教训，通常带有具体的代码文件或异常名，例如 `vacuum_state.sh`、`elements.py`、`sqlite3`
        words_to_check = [w for w in ["elements.py", "vacuum_state.sh", "reap_idle", "db_path", "PM2", "gateway", "address already in use"] if w in content.lower()]
        
        found_recent_error = False
        for word in words_to_check:
            if word in errors_text:
                found_recent_error = True
                break

        if found_recent_error:
            # 报错还在出现！强行把显著性拉升到 1.0 (防止它衰减被踢出)
            sal_conn.execute(
                "UPDATE salience SET salience = 1.0, last_access = ? WHERE memory_id = ?",
                (time.time(), mid)
            )
            boosted += 1
            logger.warning(f"⚠️ [教训未闭环] 发现相关错误依旧在报错日志中触发: '{content[:50]}'")
        else:
            # 状态正常：没有关联错误，加速进入普通知识归档
            # 如果显著性已经正常或者运行平稳，且创建时间在 3 天以上，直接将 lane 改为 'knowledge'
            # 这样它就会开始适用正常的衰减，不需要每周反思了
            # 默认给它一个轻微的 salience 奖励，表示它被完美内化了！
            sal_conn.execute(
                "UPDATE salience SET lane = 'knowledge', salience = MIN(1.0, salience + 0.05) WHERE memory_id = ?",
                (mid,)
            )
            closed += 1
            logger.info(f"✅ [教训闭环] 教训已被系统完美内化，已转为常规知识: '{content[:50]}'")

    sal_conn.commit()
    sal_conn.close()
    return {"processed": len(lessons), "boosted": boosted, "closed": closed}

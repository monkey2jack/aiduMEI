"""
ducky.routes_evolve — EvolveMem v18.1 路由
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
端点列表：
  POST /evolve/feedback   — 用户对某条记忆打反馈（有用/无用/修正）
  GET  /evolve/report     — 获取进化状态报告
  POST /evolve/cycle      — 手动触发一次进化循环（调试用）
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from ducky.evolve_mem import (
    ensure_evolve_schema,
    get_evolve_report,
    record_feedback,
    run_evolution_cycle,
)

logger = logging.getLogger("aiduMEM.routes.evolve")


class FeedbackRequest(BaseModel):
    memory_id: str = Field(..., description="记忆 UUID")
    signal: Literal["useful", "useless", "correction"] = Field(
        ..., description="反馈信号：useful=有用 / useless=无用 / correction=内容有误"
    )
    query: str = Field(default="", description="触发这条记忆的搜索词（可选）")
    correction_text: Optional[str] = Field(
        default=None,
        description="signal=correction 时，填入修正后的正确内容"
    )


def register_evolve_routes(app: FastAPI) -> None:
    """注册 EvolveMem 路由到 FastAPI 应用。"""

    # 确保 schema 就绪（幂等）
    try:
        ensure_evolve_schema()
    except Exception as e:
        logger.warning(f"EvolveMem schema 初始化警告（继续运行）: {e}")

    @app.post("/evolve/feedback", summary="记忆反馈 — 有用/无用/修正")
    def evolve_feedback(req: FeedbackRequest) -> dict:
        """
        对一条召回记忆打反馈。

        - **useful**：这条记忆回答了我的问题 → salience +0.15
        - **useless**：这条记忆不相关 → salience -0.12
        - **correction**：这条记忆内容有误（填 correction_text）→ salience +0.05（标记待修正）

        反馈立即写入 evolve_feedback 表并实时调整 salience。
        进化循环（每 6h）会汇总反馈趋势，做批量调整。
        """
        try:
            result = record_feedback(
                memory_id=req.memory_id,
                signal=req.signal,
                query=req.query,
                correction_text=req.correction_text,
            )
            return {"status": "ok", **result}
        except Exception as e:
            logger.error(f"evolve_feedback 失败: {e}", exc_info=True)
            return {"status": "error", "detail": str(e)}

    @app.get("/evolve/report", summary="EvolveMem 进化状态报告")
    def evolve_report() -> dict:
        """
        返回过去 7 天的搜索质量统计、反馈分布、salience 调整动作、上次进化时间等。
        用于监控记忆库健康度和进化进展。
        """
        try:
            return get_evolve_report()
        except Exception as e:
            logger.error(f"evolve_report 失败: {e}", exc_info=True)
            return {"status": "error", "detail": str(e)}

    @app.post("/evolve/cycle", summary="手动触发进化循环（调试）")
    def evolve_cycle() -> dict:
        """
        手动执行一次 EvolveMem 进化循环（正常由后台每 6h 自动执行）。
        返回本次 boost/decay 数量和 zero-hit 查询统计。
        """
        try:
            result = run_evolution_cycle()
            return {"status": "ok", **result}
        except Exception as e:
            logger.error(f"evolve_cycle 失败: {e}", exc_info=True)
            return {"status": "error", "detail": str(e)}

    logger.info("✅ EvolveMem 路由已注册 (/evolve/feedback · /evolve/report · /evolve/cycle)")

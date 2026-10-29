"""
ducky.routes_v8 — v8 五脉 + graduate（C 档从 api_server 抽出）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ignition · Workspace · Broadcast · J-lens · Session · Instinct graduate

语义与抽出前一致，仅搬家。
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from ducky.bank_contract import (
    DEFAULT_BANK_ID,
)
from ducky.utils import DEFAULT_USER_ID

logger = logging.getLogger("aiduMEM.routes")

# 会话结束反思默认开启；AIDUMEM_REFLECT_ON_SESSION_END=false 可关闭（手动 /reflect 不受影响）
_REFLECT_ON_SESSION_END = os.environ.get("AIDUMEM_REFLECT_ON_SESSION_END", "true").strip().lower() not in {
    "0", "false", "no", "off",
}


def register_v8_routes(app: FastAPI) -> None:


    @app.post("/session/distill")
    def session_distill(session_id: str,
                        user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID,
                        force: bool = False):
        """会话精华萃取：把这一程最值得记住的事提炼成一两句，单独存一条。

        独立于 /session/end 暴露，理由有二：
        ① 宿主的 session_end 钩子未必能拿到 /session/end 需要的那套状态
           （会话可能是被超时回收的，不是显式结束的）；
        ② 萃取要能单独重跑 —— 排查「这次怎么没精华」时，不该被迫连带
           把一个已经结束的会话再结束一次。
        本端点**只提炼，不落库** —— 返回 summary 与该带的 metadata，由调用方
        （integrations/aidumem-distill.sh）再 POST /add 写进去。这么切有两个
        实打实的好处：
        ① 精华必须进主库和向量库才能被召回（用户原话是「让最重要的东西浮出
           来」），而 /add 的完整管线在路由闭包里，服务端内部没有可复用入口；
           照反思那样落独立表会检索不到，等于没做。
        ② 纯函数语义可安全重跑：排查「这次怎么没精华」时直接再调一次即可，
           不会重复写入，也不用把一个已结束的会话再结束一遍。
        """
        try:
            from ducky.session_distill import distill_session
            return distill_session(session_id, user_id=user_id, bank_id=bank_id, force=force)
        except Exception as e:
            return {"status": "error", "detail": str(e)}



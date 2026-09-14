"""
ducky.origin_context — 溯源上下文 (v21 preview · EchoMind 融改 F2)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
每条知识演化记录回答「哪个 agent、在哪次会话、第几轮产生的」。

设计要点：
  - contextvars 传递，不写函数签名——写入路径（/add 同步、async job、
    coalesce 冲刷）在各自的 _run_pipeline/_direct_write 入口**总是先 set**
    （哪怕 set 成空值），因此不存在上一条请求的残留（stale leak）。
  - 调用方显式传入优先；缺省如实留空（"" / 0），绝不编造（诚实红线）。
  - 保留键以 `_origin_` 前缀进 metadata 透传（三条异步通路同一份 md）。

借鉴来源: EchoMind v1.2.13 的 provenance 三件套设计思想
  （origin_agent / origin_session_id / origin_turn；思路级借鉴，独立实现）。
"""
from __future__ import annotations

import contextvars
from typing import Any

_ORIGIN: contextvars.ContextVar[tuple[str, str, int]] = contextvars.ContextVar(
    "aidumei_origin", default=("", "", 0)
)

RESERVED_KEYS = ("_origin_agent", "_origin_session_id", "_origin_turn")


def set_origin(agent: str = "", session_id: str = "", turn: int = 0) -> contextvars.Token:
    return _ORIGIN.set((str(agent or ""), str(session_id or ""), int(turn or 0)))


def get_origin() -> tuple[str, str, int]:
    return _ORIGIN.get()


def reset_origin(token: contextvars.Token) -> None:
    _ORIGIN.reset(token)


def set_origin_from_metadata(meta: dict | None) -> contextvars.Token:
    """从透传 metadata 读保留键并 set（缺键=空值，**总是 set**——
    每条通路入口都先落本函数，后续写入读到的绝不会是别人的上下文）。"""
    md = meta if isinstance(meta, dict) else {}
    try:
        turn = int(md.get("_origin_turn") or 0)
    except (TypeError, ValueError):
        turn = 0
    return set_origin(
        agent=str(md.get("_origin_agent") or ""),
        session_id=str(md.get("_origin_session_id") or ""),
        turn=turn,
    )


def extract_origin_fields(extra: Any, metadata: dict | None) -> tuple[str, str, int]:
    """从请求面提取溯源三件套：顶层 extra 优先，其次 metadata 里的
    同名片段（agent / session_id / turn；兼容 hermes 风格的
    hermes_session_id）。读不到一律空值——如实，不猜。"""
    ex = extra if isinstance(extra, dict) else {}
    md = metadata if isinstance(metadata, dict) else {}

    def _pick(*names: str) -> str:
        for n in names:
            v = ex.get(n)
            if v is None:
                v = md.get(n)
            if v:
                return str(v)
        return ""

    agent = _pick("agent", "origin_agent", "client", "source_agent")
    session_id = _pick("session_id", "origin_session_id", "hermes_session_id", "session")
    raw_turn = _pick("turn", "origin_turn", "turn_index")
    try:
        turn = int(raw_turn) if raw_turn else 0
    except (TypeError, ValueError):
        turn = 0
    return agent, session_id, turn

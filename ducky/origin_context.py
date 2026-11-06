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
import re
from datetime import datetime
from typing import Any

_ORIGIN: contextvars.ContextVar[tuple[str, str, int]] = contextvars.ContextVar(
    "aidumei_origin", default=("", "", 0)
)

RESERVED_KEYS = ("_origin_agent", "_origin_session_id", "_origin_turn")

# f0.7.6（外审 R2-10）：顶层 extra 不经过 metadata 打码，会话 / agent 标识只认保守字符集。
_ORIGIN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def safe_origin_id(value: Any) -> str:
    """会话 / agent 标识：先过原则 7 打码，再要求 [A-Za-z0-9._:-]{1,128}；不合格一律丢弃（返回 ""）。"""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    from ducky.write_policy import redact_forbidden
    redacted, kinds = redact_forbidden(text)
    if kinds or redacted != text or not _ORIGIN_ID_RE.match(text):
        return ""
    return text


def safe_event_at(value: Any):
    """event_at 必须能解析成时间（ISO 8601 / 日期，或合理范围内的 epoch 秒）；否则返回 None（丢弃）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if 0 <= value < 1e11 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError:
        return None
    return value


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
    agent = str(md.get("_origin_agent") or "")
    if agent:
        from ducky import host_wiring  # f0.7 P3-4：/health 报宿主接线状态
        host_wiring.note(agent)
    return set_origin(
        agent=agent,
        session_id=str(md.get("_origin_session_id") or ""),
        turn=turn,
    )


def origin_from_metadata(meta: dict | None) -> tuple[str, str, int]:
    """从透传 metadata 的保留键读出溯源三件套；读不到才回退 contextvar。

    v21.2.0 审计整改轮（生产用户审计 🔴-1）：**显式 > 隐式**。contextvar 是个隐式通道
    ——它只在「调用方先 set 过」且「同一执行上下文」两个前提同时成立时才对。
    写入链路上任何一条通路忘了 set（或换了线程/任务），读到的就是空值，
    而空值不会报错，只会让打标静默变空。metadata 里那三个保留键是**跟着
    数据本身走**的，三条通路共享同一份 md，不依赖谁先 set 过什么。

    回退保留 contextvar，是为了不打断已经在上下文里工作的调用方（如
    reflect 那条自建上下文的路径）——但它只是兜底，不再是主通道。
    """
    md = meta if isinstance(meta, dict) else {}
    agent = str(md.get("_origin_agent") or "")
    session = str(md.get("_origin_session_id") or "")
    try:
        turn = int(md.get("_origin_turn") or 0)
    except (TypeError, ValueError):
        turn = 0
    if agent or session or turn:
        return agent, session, turn
    return get_origin()


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
                return safe_origin_id(v)
        return ""

    agent = _pick("agent", "origin_agent", "client", "source_agent")
    # v21.2.0 审计整改轮：补 conversation_id —— 写入侧（verbatim_vault）认它，
    # 检索侧不认，于是只用 conversation_id 的宿主写进去有 session、查的时候
    # 是空串，回声抑制两条腿同时静默失效。键名集合两侧必须相等。
    session_id = _pick("session_id", "origin_session_id", "hermes_session_id",
                       "conversation_id", "session")
    raw_turn = _pick("turn", "origin_turn", "turn_index")
    try:
        turn = int(raw_turn) if raw_turn else 0
    except (TypeError, ValueError):
        turn = 0
    return agent, session_id, turn


def is_session_summary(metadata: dict | None) -> bool:
    """Recognize the derived record returned by the session distill endpoint."""
    md = metadata or {}
    return (md.get("kind") == "session_distill"
            and md.get("lane") == "distill"
            and md.get("_origin_agent") == "session-distill")

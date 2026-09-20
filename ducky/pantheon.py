"""ducky.pantheon — 众神殿：多 bot/多 profile 域管理 + 跨殿借阅 (v21.1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
殿 = user_id（一个 bot/profile 一座殿）。本模块提供：
  - 殿注册表 CRUD（pantheon_halls）：创建/列出/查/停用（停用是软删，绝不碰记忆——
    生产数据生命线铁律：删殿只熄灯，不烧殿）。
  - 跨殿借阅（hall_grants）：本殿(grantor)显式授权他殿(grantee)只读/导出，
    可撤销、可过期。主体一律 user_id（殿），与 core 隔离维度对齐。

定位①（单主人多分身）：借阅是主人在自己分身之间的显式共享（如让"财务助手"读
"记账 bot"的记忆），不是零信任授权——但仍走显式 grant + 审计，让「谁能看谁」
有据可查，且默认互不可见（人格独立）。
"""
from __future__ import annotations

import logging
import time
import uuid

from ducky.utils import get_facts_conn, parse_iso_timestamp

logger = logging.getLogger("aiduMEM.pantheon")

# 借阅动作白名单——只读方向（读/导出）。众神殿①不开放跨殿写（各殿人格独立，
# 记忆只能本殿自己写；要共享内容用借阅读，不是替他殿写）。
VALID_ACTIONS = ("read", "export")


class HallError(ValueError):
    """殿管理/借阅的输入非法（空标识、非法动作、自借阅等）；路由层包成 error dict。"""


# ── 殿注册表 CRUD ─────────────────────────────────────────────────

def _norm_uid(user_id: str) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        raise HallError("殿标识 user_id 不能为空")
    if len(uid) > 200:
        raise HallError("殿标识过长（上限 200）")
    return uid


def create_hall(user_id: str, display_name: str = "", description: str = "") -> dict:
    """创建或更新一座殿（幂等：同 user_id 再建即更新元数据）。"""
    uid = _norm_uid(user_id)
    conn = get_facts_conn()
    try:
        conn.execute(
            """INSERT INTO pantheon_halls (user_id, display_name, description)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   display_name=excluded.display_name,
                   description=excluded.description,
                   active=1,
                   updated_at=CURRENT_TIMESTAMP""",
            (uid, str(display_name or ""), str(description or "")),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("众神殿：殿 %s 已登记/更新", uid)
    return get_hall(uid) or {}


def get_hall(user_id: str) -> dict | None:
    uid = _norm_uid(user_id)
    conn = get_facts_conn()
    try:
        row = conn.execute(
            "SELECT user_id, display_name, description, active, created_at "
            "FROM pantheon_halls WHERE user_id=?", (uid,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"user_id": row[0], "display_name": row[1], "description": row[2],
            "active": bool(row[3]), "created_at": row[4]}


def list_halls(include_inactive: bool = False) -> list[dict]:
    conn = get_facts_conn()
    try:
        sql = ("SELECT user_id, display_name, description, active, created_at "
               "FROM pantheon_halls")
        if not include_inactive:
            sql += " WHERE active=1"
        sql += " ORDER BY created_at ASC"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    return [{"user_id": r[0], "display_name": r[1], "description": r[2],
             "active": bool(r[3]), "created_at": r[4]} for r in rows]


def deactivate_hall(user_id: str) -> dict:
    """停用一座殿（软删：active=0，绝不删记忆——生产数据生命线）。
    连带撤销该殿【授出】的借阅（熄灯即收回钥匙），但保留【收到】的借阅记录（审计）。"""
    uid = _norm_uid(user_id)
    conn = get_facts_conn()
    try:
        cur = conn.execute(
            "UPDATE pantheon_halls SET active=0, updated_at=CURRENT_TIMESTAMP "
            "WHERE user_id=? AND active=1", (uid,))
        # 撤销该殿授出的、尚未撤销的借阅
        conn.execute(
            "UPDATE hall_grants SET revoked_at=CURRENT_TIMESTAMP "
            "WHERE grantor_user_id=? AND revoked_at IS NULL", (uid,))
        conn.commit()
        changed = cur.rowcount
    finally:
        conn.close()
    logger.info("众神殿：殿 %s 已停用（软删，记忆无损）", uid)
    return {"user_id": uid, "deactivated": bool(changed)}


# ── 跨殿借阅 ───────────────────────────────────────────────────────

def _normalize_actions(actions) -> str:
    if isinstance(actions, str):
        items = [a.strip() for a in actions.split(",") if a.strip()]
    else:
        items = [str(a).strip() for a in (actions or []) if str(a).strip()]
    if not items:
        raise HallError("借阅动作不能为空")
    for a in items:
        if a not in VALID_ACTIONS:
            raise HallError(f"非法借阅动作 {a!r}（合法：{VALID_ACTIONS}）")
    # 去重保序
    seen, out = set(), []
    for a in items:
        if a not in seen:
            seen.add(a); out.append(a)
    return ",".join(out)


def grant_hall_access(grantor_user_id: str, grantee_user_id: str,
                      actions="read", bank_id: str = "*",
                      expires_at: str | None = None, created_by: str = "") -> dict:
    """本殿(grantor)授权他殿(grantee)借阅。返回 grant dict。

    - grantor==grantee 拒绝（不给自己发借阅——本殿访问自己天然放行）。
    - actions 白名单校验；bank_id='*' 表示殿内所有库，否则限定单库。
    - expires_at 非法值创建即拒（安全特性降级方向朝更严）。
    """
    gr = _norm_uid(grantor_user_id)
    ge = _norm_uid(grantee_user_id)
    if gr == ge:
        raise HallError("不能给自己所在的殿发借阅（本殿访问自己无需授权）")
    acts = _normalize_actions(actions)
    bank = str(bank_id or "*").strip() or "*"
    if expires_at:
        ts = parse_iso_timestamp(str(expires_at))
        if not ts or ts <= time.time():
            raise HallError("expires_at 非法或已是过去时刻（借阅创建即拒，不留失效授权）")
    gid = f"hg_{uuid.uuid4().hex[:16]}"
    conn = get_facts_conn()
    try:
        conn.execute(
            """INSERT INTO hall_grants
               (grant_id, grantor_user_id, grantee_user_id, actions, bank_id,
                expires_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (gid, gr, ge, acts, bank, expires_at or None, str(created_by or "")),
        )
        # v21.2 M8：借阅授权进事件账本留痕（来源标记 = 哪端发起）。
        # 与事实变更同一纪律：在调用方事务内 INSERT，随后一起 commit ——
        # 授权成功而账没记上，等于留了一条查不到出处的权限。
        try:
            from ducky.event_ledger import record_event
            record_event(conn, actor=str(created_by or gr), action="hall_grant",
                         target_id=gid,
                         reason=f"grantor={gr} grantee={ge} actions={acts} bank={bank}",
                         user_id=gr, bank_id=bank if bank != "*" else "")
        except Exception as _le:
            logger.debug("借阅留痕跳过: %s", _le)
        conn.commit()
    finally:
        conn.close()
    logger.info("众神殿借阅：%s 授权 %s（%s，bank=%s）", gr, ge, acts, bank)
    return {"grant_id": gid, "grantor_user_id": gr, "grantee_user_id": ge,
            "actions": acts.split(","), "bank_id": bank, "expires_at": expires_at}


def revoke_hall_grant(grant_id: str) -> dict:
    """撤销一条借阅（终态：revoked_at 一旦置就是终态，重授须发新 grant_id）。"""
    gid = str(grant_id or "").strip()
    if not gid:
        raise HallError("grant_id 不能为空")
    conn = get_facts_conn()
    try:
        cur = conn.execute(
            "UPDATE hall_grants SET revoked_at=CURRENT_TIMESTAMP "
            "WHERE grant_id=? AND revoked_at IS NULL", (gid,))
        revoked = cur.rowcount
        # v21.2 M8：只有**真撤到了**才记账 —— 对一条早已撤销（或不存在）的
        # grant 记一笔「已撤销」，是往账本里写一件没发生过的事。
        if revoked:
            try:
                from ducky.event_ledger import record_event
                record_event(conn, actor="revoke", action="hall_grant_revoke",
                             target_id=gid, reason="借阅撤销（终态）")
            except Exception as _le:
                logger.debug("借阅撤销留痕跳过: %s", _le)
        conn.commit()
    finally:
        conn.close()
    return {"grant_id": gid, "revoked": bool(revoked)}


def check_hall_access(grantor_user_id: str, grantee_user_id: str,
                      action: str = "read", bank_id: str | None = None) -> bool:
    """grantee 能否对 grantor 的殿执行 action？

    - 本殿访问自己（grantor==grantee）恒 True。
    - 否则需一条有效借阅：未撤销 + 未过期 + action 覆盖 + bank 覆盖。
    - 过期时间解析失败/非有限 → fail-closed（视为无效，拒绝）——安全判据朝更严。
    """
    gr = str(grantor_user_id or "").strip()
    ge = str(grantee_user_id or "").strip()
    if not gr or not ge:
        return False
    if gr == ge:
        return True
    act = str(action or "read").strip()
    want_bank = None if bank_id is None else (str(bank_id).strip() or "*")
    now = time.time()
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            "SELECT actions, bank_id, expires_at FROM hall_grants "
            "WHERE grantor_user_id=? AND grantee_user_id=? AND revoked_at IS NULL",
            (gr, ge)).fetchall()
    finally:
        conn.close()
    for acts, gbank, exp in rows:
        # 过期判据：有 expires_at 就必须解析成有限且未来的时刻，否则 fail-closed 跳过
        if exp:
            ts = parse_iso_timestamp(str(exp))
            if not ts or ts <= now:   # 解析失败(0.0/None)或已过期 → 此条无效
                continue
        grant_acts = {a.strip() for a in str(acts or "").split(",") if a.strip()}
        if act not in grant_acts:
            continue
        # bank 覆盖：grant bank='*' 覆盖全部；否则须与请求 bank 精确一致
        if want_bank is not None and str(gbank) != "*" and str(gbank) != want_bank:
            continue
        return True
    return False


def authorize_cross_hall(target_user_id: str, caller_user_id: str,
                         bank_id: str | None = None, action: str = "read") -> bool:
    """跨殿读授权（core 读路径的织入点）：caller 想 action 读 target 的殿。

    v22.0（雷霆审计 A3）收紧：空 caller 不再对所有人放行——
    - caller==target → 放行（读自己殿）；
    - caller 为空 且 凭据是 UI 会话（主人直连）或未过鉴权中间件（回环单主人
      自用）→ 放行（①定位语义保留）；
    - caller 为空 但凭据是 API token（agent/集成）→ 拒绝：持 token 的调用方
      必须声明自己是哪座殿，「不声明身份」不再是绕行通道。

    此前空 caller 一律放行，意味着任何持共享 token 的参与者只要**省略**
    caller_user_id 就能读任意殿——v21.1「跨殿默认隔离」的承诺被一个缺省
    参数拆掉。收紧后：省略 caller 只对主人（session/回环）成立。
    """
    tgt = str(target_user_id or "").strip()
    clr = str(caller_user_id or "").strip()
    if clr == tgt:
        return True
    if not clr:
        from ducky.security.auth import current_request_auth_kind
        kind = current_request_auth_kind()
        if kind == "bearer":
            raise HallError(
                "API token 调用方必须声明 caller_user_id——"
                "空 caller 仅对主人直连（UI 会话/回环）放行"
            )
        return True  # session 或未经鉴权中间件（回环单主人）：①定位语义
    if check_hall_access(tgt, clr, action=action, bank_id=bank_id):
        return True
    raise HallError(f"殿「{clr}」未获殿「{tgt}」的 {action} 借阅——跨殿访问默认隔离，请先取得借阅")


def list_hall_grants(user_id: str, direction: str = "granted") -> list[dict]:
    """列出某殿的借阅。direction='granted'=本殿授出；'received'=本殿收到。"""
    uid = _norm_uid(user_id)
    col = "grantor_user_id" if direction == "granted" else "grantee_user_id"
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            f"SELECT grant_id, grantor_user_id, grantee_user_id, actions, bank_id, "
            f"expires_at, revoked_at FROM hall_grants WHERE {col}=? "
            f"ORDER BY created_at DESC", (uid,)).fetchall()
    finally:
        conn.close()
    return [{"grant_id": r[0], "grantor_user_id": r[1], "grantee_user_id": r[2],
             "actions": str(r[3] or "").split(","), "bank_id": r[4],
             "expires_at": r[5], "revoked": r[6] is not None} for r in rows]

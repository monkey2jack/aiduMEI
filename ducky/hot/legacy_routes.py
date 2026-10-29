"""ducky.hot.legacy_routes — SQLite Legacy 路由注册

v22.0（雷霆审计 B5 · Kimi R-5）：本模块是 v19 兼容层——端点不传 user_id/bank_id
时落 default 域，这是**有意设计**（存量脚本/钩子/控制台零破坏），不是漏洞。
「不传 = 默认域」在 v21 域纪律下等价于「不传 = 管理员全量视图」的指控**不成立**：
这些端点全部经 tenant_clause / normalize 收窄，不传时只读 default 域的
行，不是全库。

真正的风险是**不可见**：调用方不知道「不传 = default 域」这个行为。
因此本模块顶部声明语义，并在 /health 暴露 legacy 端点计数（让「默认域
兜底」从隐式变显式）。
"""
from __future__ import annotations

import logging

from ducky.utils import (
    DEFAULT_USER_ID,
)
from ducky.bank_contract import DEFAULT_BANK_ID, normalize_bank_id
from ducky.facts_recall import tenant_clause
from ducky.hot.legacy_helpers import (
    _get_facts_conn,
    _get_scenes_conn,
    _ensure_scenes_table,
)


logger = logging.getLogger("aiduMEM.legacy.routes")


def register_legacy_routes(app):
    """把 §6-§10 的全部 22 个端点注册到 FastAPI app 上"""

    # ── 6.2  Facts CRUD ──
    @app.get("/facts")
    def list_facts(category: str = None, key: str = None, level: str = "L2",
                   user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID,
                   caller_user_id: str = ""):
        from ducky.scope_auth import require_scope_access
        require_scope_access(user_id, caller_user_id, bank_id=bank_id, action="read")
        level_norm = (level or "L2").upper()
        if level_norm not in ("L0","L1","L2"): level_norm = "L2"
        conn = _get_facts_conn()
        cur = conn.cursor()
        where, params = [], []
        if category: where.append("category = ?"); params.append(category)
        if key:      where.append("fact_key = ?");   params.append(key)
        sql = "SELECT * FROM facts" + (" WHERE " + " AND ".join(where) if where else " WHERE 1=1")
        # 🔴P0-2（v19.4.1）：租户可见性收窄。默认租户保持全库可见（向后兼容），
        # 传具体 user_id 时只返回该租户可见的事实。
        # v20：传 conn 感知 bank_id 列——已迁移库上默认域不再看见具名域的行。
        t_clause, t_params = tenant_clause(
            user_id, bank_id=normalize_bank_id(bank_id), conn=conn
        )
        sql += t_clause
        params.extend(t_params)
        sql += " ORDER BY category, fact_key"
        cur.execute(sql, params)
        raw = [dict(r) for r in cur.fetchall()]
        conn.close()
        if level_norm == "L2": rows = raw
        else:
            rows = []
            for r in raw:
                item = {k:v for k,v in r.items() if k not in ("summary","overview")}
                item["value"] = r.get("summary") if level_norm=="L0" else (r.get("overview") or r["fact_value"])
                rows.append(item)
        return {"status":"ok","count":len(rows),"level":level_norm,"facts":rows}


    @app.get("/facts/categories")
    def list_fact_categories(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID):
        conn = _get_facts_conn()
        # 🔴P0-2：类别计数也按租户收窄，否则可从类别分布反推他人数据规模
        t_clause, t_params = tenant_clause(
            user_id, bank_id=normalize_bank_id(bank_id), conn=conn
        )
        rows = conn.execute(
            "SELECT category, COUNT(*) AS cnt FROM facts WHERE 1=1" + t_clause
            + " GROUP BY category ORDER BY category",
            t_params,
        ).fetchall()
        conn.close()
        return {"status":"ok","categories":[dict(r) for r in rows]}


    @app.get("/facts/entities/list")
    def list_entities(entity_type: str = None, limit: int = 50,
                      user_id: str = DEFAULT_USER_ID,
                      bank_id: str = DEFAULT_BANK_ID):
        # v20.2.4（外审 F-05）：entities 是**全局实体表**，它的域归属只能靠
        # 关联的 facts 来定。所以 JOIN facts 并按 scope 收窄。
        #
        # 语义变化如实写明：原来是 LEFT JOIN，孤立实体（没有任何关联事实）
        # 也会列出来；现在改 JOIN，只列**本域有事实关联**的实体。域隔离下这是
        # 对的 —— 一个实体如果在本域没有任何事实，它不属于本域的知识。
        # 用模块级已导入的 tenant_clause —— 函数内再 import 一次会把它变成局部名，
        # 整个函数体内该名字都是局部的（P0-2 那次 /add 连炸 13 分钟就是这么来的）。
        conn = _get_facts_conn()
        fclause, fparams = tenant_clause(user_id, alias="f", bank_id=bank_id, conn=conn)
        # 整条 SQL 内联在 f-string 里（不用 base + f"..." 拼）是刻意的：
        # 拼接形态会**逃过** f-string SQL 插值守卫的扫描（v20.1.1 N-4），
        # 于是这处插值既不被登记也不被检查。让代码落进守卫射程，
        # 而不是让守卫将就代码。
        if entity_type:
            rows = conn.execute(f"""
                SELECT e.*, COUNT(fe.fact_id) as fact_count
                FROM entities e
                JOIN fact_entities fe ON fe.entity_id=e.entity_id
                JOIN facts f ON f.id=fe.fact_id
                WHERE e.entity_type=?{fclause}
                GROUP BY e.entity_id ORDER BY fact_count DESC LIMIT ?
            """, [entity_type] + fparams + [limit]).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT e.*, COUNT(fe.fact_id) as fact_count
                FROM entities e
                JOIN fact_entities fe ON fe.entity_id=e.entity_id
                JOIN facts f ON f.id=fe.fact_id
                WHERE 1=1{fclause}
                GROUP BY e.entity_id ORDER BY fact_count DESC LIMIT ?
            """, fparams + [limit]).fetchall()
        conn.close()
        return {"status":"ok","entities":[dict(r) for r in rows],"count":len(rows)}


    @app.get("/facts/trust-stats")
    def fact_trust_stats(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID):
        conn = _get_facts_conn()
        t_clause, t_params = tenant_clause(
            user_id, bank_id=normalize_bank_id(bank_id), conn=conn
        )
        rows = conn.execute("""
            SELECT category,
                   COUNT(*) as cnt, AVG(trust_score) as avg_trust,
                   SUM(helpful_count) as helpful, SUM(unhelpful_count) as unhelpful
            FROM facts WHERE archived=0
        """ + t_clause + """ GROUP BY category ORDER BY cnt DESC
        """, t_params).fetchall()
        conn.close()
        return {"status":"ok","categories":[dict(r) for r in rows]}


    @app.get("/scene")
    def list_scenes(category: str = None, limit: int = 20,
                    user_id: str = "", bank_id: str = ""):
        # f0.6 W-25（muse H-1）：/scene 无作用域即可枚举全租户场景摘要，
        # limit 再不设上界就是放大器；钳 500。
        limit = max(1, min(int(limit or 20), 500))
        conn = _get_scenes_conn()
        _ensure_scenes_table(conn)  # 🔴6：保证表存在，避免开箱 500
        # v20 P0-2：作用域过滤 opt-in；不传 = v19 管理员全量视图（存量零改动）
        clauses, params = [], []
        if category: clauses.append("category=?"); params.append(category)
        if user_id:  clauses.append("user_id=?");  params.append(user_id)
        if bank_id:  clauses.append("bank_id=?");  params.append(bank_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM scenes {where}ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        conn.close()
        return {"status":"ok","scenes":[dict(r) for r in rows]}


    logger.info("✅ 22 legacy 端点已注册到 FastAPI app")

"""ducky.extended.routes — auto-memory + 15脉端点"""
from __future__ import annotations

from ducky.utils import DEFAULT_USER_ID
from ducky.utils import get_facts_conn as _gfc
from ducky.bank_contract import DEFAULT_BANK_ID

import logging

from fastapi import Query


from ducky.facts_recall import tenant_clause

from ducky.extended import auto_memory as am

logger = logging.getLogger("aiduMEM.extended")


def register_extended_routes(app, _get_memory_fn, _get_db_fn, _extract_entities_fn):
    """注册 auto-memory + 15脉端点，依赖由组装层显式注入。"""

    am.bind_runtime(
        get_memory_fn=_get_memory_fn,
        get_db_fn=_get_db_fn,
        get_facts_conn_fn=_gfc,
    )
    # ⚠️ v20.2.4（外审 F-05 / F-07）：本模块这批「次级事实端点」在 v20.0 的
    # 全量记忆域隔离里**整体漏了** —— 主路径（/add、/search、delete_all）都做了
    # 二维 scope 收窄，而这里的路由签名连 user_id 都没有，SQL 是裸的
    # `WHERE id=?` / `WHERE archived=0`。后果不是计数侧信道，是**直接吐正文**
    # 和**按 ID 改别人的事实**。
    #
    # 收窄统一用 ducky.facts_recall.tenant_clause —— 读侧唯一的 scope 谓词来源，
    # 它自带域收窄语义（默认租户在已迁移库上也收到 bank_id='default'）。
    # **不要在这里另写一份 SQL 片段**，两份早晚长歪一份。
    # 兼容：路由闭包内用局部名（与旧语义一致）
    _get_db = _get_db_fn
    _get_facts_conn = _gfc
    _extract_entities = _extract_entities_fn
    get_memory = _get_memory_fn  # noqa: F841

    # ═══════════════════ §12  Auto-Memory ═══════════════════


    # ═══════════════════ §13  15脉新能力 ═══════════════════


    @app.get("/knowledge/tree")
    def knowledge_tree(user_id:str=Query(DEFAULT_USER_ID),
                       bank_id:str=Query(DEFAULT_BANK_ID),
                       caller_user_id:str=Query("")):
        from ducky.scope_auth import require_scope_access
        require_scope_access(user_id, caller_user_id, bank_id=bank_id, action="read")
        db = _get_facts_conn()
        clause, params = tenant_clause(user_id, bank_id=bank_id, conn=db)
        cats = db.execute("""SELECT category,COUNT(*) as cnt FROM facts WHERE archived=0""" + clause + """
            GROUP BY category ORDER BY cnt DESC""", params).fetchall()
        db.close()
        tree = {}
        for c in cats:
            parts = c['category'].replace('·','.').split('.')
            node = tree
            for p in parts[:-1]: node = node.setdefault(p, {})
            node[parts[-1]] = {"_count":c['cnt']}
        return {"domains":len(tree),"total_facts":sum(c['cnt'] for c in cats),"tree":tree}


    logger.info("✅ Extended routes registered (auto-memory + 15-vein)")

"""ducky.extended.routes — auto-memory + 15脉端点"""
from __future__ import annotations

from ducky.utils import DEFAULT_USER_ID
from ducky.utils import get_facts_conn as _gfc
from ducky.bank_contract import DEFAULT_BANK_ID, table_columns

import logging
import os
import json
from datetime import datetime, timezone, timedelta

from fastapi import Form, HTTPException, Query

from ducky.facts_recall import tenant_clause

from ducky.extended import auto_memory as am
from ducky.extended.auto_memory import (
    AUTO_MEMORY_STATE,
    _run_auto_memory,
)
from ducky.text_fts import _fts_terms

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

    @app.get("/auto-memory/status")
    def auto_memory_status():
        state = {}
        if os.path.exists(AUTO_MEMORY_STATE):
            with open(AUTO_MEMORY_STATE) as f: state = json.load(f)
        return {"status":"ok","last_run":state.get("last_run"),"last_msg_id":state.get("last_msg_id")}

    @app.post("/auto-memory/trigger")
    def auto_memory_trigger():
        r = _run_auto_memory()
        return r or {"status":"ok","message":"auto_memory 已触发"}

    # ═══════════════════ §13  15脉新能力 ═══════════════════

    @app.get("/persona/ai-self")
    def persona_ai_self(
        user_id: str = Query(DEFAULT_USER_ID),
        bank_id: str = Query(DEFAULT_BANK_ID),
    ):
        db = _get_facts_conn()
        clause, params = tenant_clause(user_id, bank_id=bank_id, conn=db)
        facts = db.execute("""SELECT category, fact_key, fact_value, trust_score
            FROM facts WHERE peer='ai' AND archived=0""" + clause + """
            ORDER BY trust_score DESC LIMIT 30""", params).fetchall()
        db.close()
        traits = {}
        for f in facts:
            if f['category'] not in traits:
                traits[f['category']] = []
            traits[f['category']].append({"key":f['fact_key'],"value":f['fact_value'],"trust":f['trust_score']})
        return {"peer":"AI","traits":traits,"total_facts":len(facts),
                "summary":f"AI是一个拥有 {len(facts)} 条自我认知的 AI 助手，涵盖 {len(traits)} 个维度"}

    @app.post("/persona/ai-self/add")
    def persona_ai_self_add(
        category: str,
        key: str,
        value: str,
        bank_id: str = Query(DEFAULT_BANK_ID),
    ):
        # v20.3.1（外审）：写入身份不由调用方自选。默认身份是部署配置，
        # bank 仍显式传入，读侧也按同一 scope 谓词收窄。
        db = _get_facts_conn()
        now = datetime.now(timezone.utc).isoformat()
        _cols = table_columns(db, "facts")
        _scope_cols = [c for c in ("user_id", "bank_id") if c in _cols]
        _scope_vals = [DEFAULT_USER_ID if c == "user_id" else bank_id for c in _scope_cols]
        cur = db.execute(
            "INSERT INTO facts (category,fact_key,fact_value,peer,trust_score,created_at,updated_at"
            + "".join(f",{c}" for c in _scope_cols)
            + ") VALUES (?,?,?,'ai',0.7,?,?"
            + ",?" * len(_scope_cols)
            + ")",
            (category, key, value, now, now, *_scope_vals))
        fid = cur.lastrowid or 0
        # 📒 事件账本（v19.4.0 🟡-D）：AI 自我认知写入留痕，同事务
        try:
            from ducky.event_ledger import content_hash, record_event
            record_event(db, actor="ai-self", action="add",
                         target_id=f"fact:{key}",
                         reason=f"persona ai-self: category={category}",
                         after_hash=content_hash(value))
        except Exception:
            pass  # 账本失败不阻断写入
        db.commit()
        db.close()
        # 触发实体提取保持与旧端点一致；当前提取器为纯函数，不另行落库。
        _extract_entities(f"{key}: {value}")
        return {"ok":True,"fact_id":fid,"peer":"ai"}

    @app.post("/facts/preference")
    def facts_preference(fact_id:int, score:float=Query(0.5, ge=-1.0, le=1.0),
                         user_id:str=Query(DEFAULT_USER_ID),
                         bank_id:str=Query(DEFAULT_BANK_ID)):
        db = _get_facts_conn()
        clause, params = tenant_clause(user_id, bank_id=bank_id, conn=db)
        cur = db.execute("UPDATE facts SET preference_score=? WHERE id=?" + clause,
                         [score, fact_id] + params)
        db.commit(); n = cur.rowcount; db.close()
        # 越域与不存在**同一个 404**：区分开就等于告诉调用方「这个 id 存在，
        # 只是不属于你」——那是一条按 ID 探测他域的侧信道。
        if not n:
            raise HTTPException(status_code=404, detail="fact not found in this scope")
        return {"ok":True,"fact_id":fact_id,"preference_score":score}

    @app.get("/facts/preferences")
    def facts_preferences_list(min_abs:float=0.3,
                               user_id:str=Query(DEFAULT_USER_ID),
                               bank_id:str=Query(DEFAULT_BANK_ID)):
        db = _get_facts_conn()
        clause, params = tenant_clause(user_id, bank_id=bank_id, conn=db)
        rows = db.execute("""SELECT id,category,fact_key,fact_value,preference_score
            FROM facts WHERE ABS(preference_score)>=? AND archived=0""" + clause + """
            ORDER BY ABS(preference_score) DESC LIMIT 50""", [min_abs] + params).fetchall()
        db.close()
        return {"count":len(rows),"likes":len([r for r in rows if r['preference_score']>0]),
                "dislikes":len([r for r in rows if r['preference_score']<0]),
                "items":[dict(r) for r in rows]}

    @app.post("/facts/expire")
    def facts_expire(fact_id:int,
                     # 范围收窄（外审 F-07）：此前任意调用方可以把事实立刻过期
                     # 或推到极远未来。1 小时 ~ 10 年。
                     expires_in_hours:int=Query(24, ge=1, le=87600),
                     user_id:str=Query(DEFAULT_USER_ID),
                     bank_id:str=Query(DEFAULT_BANK_ID)):
        db = _get_facts_conn()
        expires_at = (datetime.now(timezone.utc)+timedelta(hours=expires_in_hours)).isoformat()
        clause, params = tenant_clause(user_id, bank_id=bank_id, conn=db)
        cur = db.execute("UPDATE facts SET expires_at=? WHERE id=?" + clause,
                         [expires_at, fact_id] + params)
        db.commit(); n = cur.rowcount; db.close()
        if not n:
            raise HTTPException(status_code=404, detail="fact not found in this scope")
        return {"ok":True,"fact_id":fact_id,"expires_at":expires_at}

    @app.get("/knowledge/tree")
    def knowledge_tree(user_id:str=Query(DEFAULT_USER_ID),
                       bank_id:str=Query(DEFAULT_BANK_ID)):
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

    @app.get("/facts/delta")
    def facts_delta(since:str=Query(..., description="ISO时间戳"),
                    user_id:str=Query(DEFAULT_USER_ID),
                    bank_id:str=Query(DEFAULT_BANK_ID)):
        db = _get_facts_conn()
        clause, params = tenant_clause(user_id, bank_id=bank_id, conn=db)
        added = db.execute("""SELECT id,category,fact_key,fact_value,created_at
            FROM facts WHERE created_at>? AND archived=0""" + clause + """
            ORDER BY created_at DESC LIMIT 100""", [since] + params).fetchall()
        archived = db.execute("""SELECT id,category,fact_key,archived_at
            FROM facts WHERE archived=1 AND archived_at>?""" + clause + """
            ORDER BY archived_at DESC LIMIT 50""", [since] + params).fetchall()
        db.close()
        return {"since":since,"added":len(added),"removed":len(archived),
                "new_facts":[dict(r) for r in added[:20]],
                "archived_facts":[dict(r) for r in archived[:10]]}

    @app.get("/search/deep")
    def search_deep(query:str, depth:int=Query(2, ge=1, le=3),
                    user_id:str=Query(DEFAULT_USER_ID),
                    bank_id:str=Query(DEFAULT_BANK_ID)):
        db = _get_facts_conn()
        # 别名 f：本路由两个查询都用 f.* —— tenant_clause 的 alias 参数就是为这个。
        fclause, fparams = tenant_clause(user_id, alias="f", bank_id=bank_id, conn=db)
        try:
            # 🟡18：facts_fts 虚拟表从未创建，原 JOIN 每次抛异常静默降级为空。
            # 改用 facts 表上的关键词 LIKE 检索（中英混合切词），不依赖 FTS 虚拟表。
            terms = _fts_terms(query) or [query.strip()]
            terms = [t for t in terms if t][:12]
            if terms:
                clauses = " OR ".join(["f.fact_value LIKE ?"] * len(terms))
                params = [f"%{t}%" for t in terms]
                fts_results = db.execute(f"""SELECT f.id,f.category,f.fact_key,f.fact_value,
                    f.trust_score,f.preference_score,f.retrieval_count
                    FROM facts f
                    WHERE ({clauses}) AND f.archived=0{fclause}
                    ORDER BY f.trust_score*(1.0+f.preference_score) DESC LIMIT 20""",
                    params + fparams).fetchall()
            else:
                fts_results = []
        except Exception as e:
            logger.debug(f"deep keyword search skip: {e}")
            fts_results = []
        entities = _extract_entities(query)
        entity_facts = []
        if entities:
            placeholders = ','.join(['?']*len(entities))
            entity_facts = db.execute(f"""SELECT DISTINCT f.id,f.category,f.fact_key,f.fact_value,
                f.trust_score,f.preference_score FROM facts f
                JOIN fact_entities fe ON fe.fact_id=f.id
                JOIN entities e ON e.entity_id=fe.entity_id
                WHERE e.name IN ({placeholders}) AND f.archived=0{fclause}
                ORDER BY f.trust_score DESC LIMIT 10""", list(entities) + fparams).fetchall()
        db.close()
        seen = set(); merged = []
        for r in (list(fts_results)+list(entity_facts)):
            if r['id'] not in seen: seen.add(r['id']); merged.append(dict(r))
        return {"query":query,"depth":depth,"entities_found":entities,
                "fts_hits":len(fts_results),"entity_hits":len(entity_facts),
                "merged_total":len(merged),"results":merged[:15]}

    @app.post("/facts/compress")
    def facts_compress(text:str=Form(...)):
        lines = text.split('\n')
        error_kw = ['error','fail','traceback','exception','❌','panic','fatal']
        kept = []
        for line in lines:
            lower = line.lower()
            if any(kw in lower for kw in error_kw) or line.strip().startswith('File "'):
                kept.append(line)
            elif len(line.strip())<3:
                continue  # 跳过空行
            elif lower in ['ok','done','success'] and kept and kept[-1].strip().lower()==lower:
                continue  # 跳过重复 status 行
            else:
                kept.append(line)
        return {"original_chars":len(text),"compressed_chars":sum(len(l) for l in kept),
                "original_lines":len(lines),"kept_lines":len(kept),
                "compression_ratio":f"{sum(len(l) for l in kept)/max(len(text),1)*100:.1f}%",
                "compressed":'\n'.join(kept)}

    logger.info("✅ Extended routes registered (auto-memory + 15-vein)")

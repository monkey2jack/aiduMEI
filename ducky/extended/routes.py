"""ducky.extended.routes — auto-memory + 15脉端点"""
from __future__ import annotations

from ducky.utils import get_facts_conn as _gfc

import logging
import os
import json
from datetime import datetime, timezone, timedelta

from fastapi import Form, Query

from ducky.extended import auto_memory as am
from ducky.extended.auto_memory import (
    AUTO_MEMORY_STATE,
    _run_auto_memory,
)

logger = logging.getLogger("aiduMEM.extended")


def register_extended_routes(app, _get_memory_fn, _get_db_fn, _extract_entities_fn):
    """注册 auto-memory + 15脉端点，依赖由组装层显式注入。"""

    am.bind_runtime(
        get_memory_fn=_get_memory_fn,
        get_db_fn=_get_db_fn,
        get_facts_conn_fn=_gfc,
    )
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
    def persona_ai_self():
        db = _get_facts_conn()
        facts = db.execute("""SELECT category, fact_key, fact_value, trust_score
            FROM facts WHERE peer='ai' AND archived=0
            ORDER BY trust_score DESC LIMIT 30""").fetchall()
        db.close()
        traits = {}
        for f in facts:
            if f['category'] not in traits:
                traits[f['category']] = []
            traits[f['category']].append({"key":f['fact_key'],"value":f['fact_value'],"trust":f['trust_score']})
        return {"peer":"AI","traits":traits,"total_facts":len(facts),
                "summary":f"AI是一个拥有 {len(facts)} 条自我认知的 AI 助手，涵盖 {len(traits)} 个维度"}

    @app.post("/persona/ai-self/add")
    def persona_ai_self_add(category:str, key:str, value:str):
        db = _get_facts_conn()
        now = datetime.now(timezone.utc).isoformat()
        db.execute("""INSERT INTO facts (category,fact_key,fact_value,peer,trust_score,created_at,updated_at)
            VALUES (?,?,?,'ai',0.7,?,?)""", (category,key,value,now,now))
        db.commit()
        fid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        # 触发实体提取保持与旧端点一致；当前提取器为纯函数，不另行落库。
        _extract_entities(f"{key}: {value}")
        return {"ok":True,"fact_id":fid,"peer":"ai"}

    @app.post("/facts/preference")
    def facts_preference(fact_id:int, score:float=Query(0.5, ge=-1.0, le=1.0)):
        db = _get_facts_conn()
        db.execute("UPDATE facts SET preference_score=? WHERE id=?", (score, fact_id))
        db.commit(); db.close()
        return {"ok":True,"fact_id":fact_id,"preference_score":score}

    @app.get("/facts/preferences")
    def facts_preferences_list(min_abs:float=0.3):
        db = _get_facts_conn()
        rows = db.execute("""SELECT id,category,fact_key,fact_value,preference_score
            FROM facts WHERE ABS(preference_score)>=? AND archived=0
            ORDER BY ABS(preference_score) DESC LIMIT 50""", (min_abs,)).fetchall()
        db.close()
        return {"count":len(rows),"likes":len([r for r in rows if r['preference_score']>0]),
                "dislikes":len([r for r in rows if r['preference_score']<0]),
                "items":[dict(r) for r in rows]}

    @app.post("/facts/expire")
    def facts_expire(fact_id:int, expires_in_hours:int=24):
        db = _get_facts_conn()
        expires_at = (datetime.now(timezone.utc)+timedelta(hours=expires_in_hours)).isoformat()
        db.execute("UPDATE facts SET expires_at=? WHERE id=?", (expires_at, fact_id))
        db.commit(); db.close()
        return {"ok":True,"fact_id":fact_id,"expires_at":expires_at}

    @app.get("/knowledge/tree")
    def knowledge_tree():
        db = _get_facts_conn()
        cats = db.execute("""SELECT category,COUNT(*) as cnt FROM facts WHERE archived=0
            GROUP BY category ORDER BY cnt DESC""").fetchall()
        db.close()
        tree = {}
        for c in cats:
            parts = c['category'].replace('·','.').split('.')
            node = tree
            for p in parts[:-1]: node = node.setdefault(p, {})
            node[parts[-1]] = {"_count":c['cnt']}
        return {"domains":len(tree),"total_facts":sum(c['cnt'] for c in cats),"tree":tree}

    @app.get("/facts/delta")
    def facts_delta(since:str=Query(..., description="ISO时间戳")):
        db = _get_facts_conn()
        added = db.execute("""SELECT id,category,fact_key,fact_value,created_at
            FROM facts WHERE created_at>? AND archived=0
            ORDER BY created_at DESC LIMIT 100""", (since,)).fetchall()
        archived = db.execute("""SELECT id,category,fact_key,archived_at
            FROM facts WHERE archived=1 AND archived_at>?
            ORDER BY archived_at DESC LIMIT 50""", (since,)).fetchall()
        db.close()
        return {"since":since,"added":len(added),"removed":len(archived),
                "new_facts":[dict(r) for r in added[:20]],
                "archived_facts":[dict(r) for r in archived[:10]]}

    @app.get("/search/deep")
    def search_deep(query:str, depth:int=Query(2, ge=1, le=3)):
        db = _get_facts_conn()
        try:
            fts_results = db.execute("""SELECT f.id,f.category,f.fact_key,f.fact_value,
                f.trust_score,f.preference_score,f.retrieval_count
                FROM facts f JOIN facts_fts ft ON f.id=ft.rowid
                WHERE facts_fts MATCH ? AND f.archived=0
                ORDER BY f.trust_score*(1.0+f.preference_score) DESC LIMIT 20""", (query,)).fetchall()
        except Exception:
            fts_results = []
        entities = _extract_entities(query)
        entity_facts = []
        if entities:
            placeholders = ','.join(['?']*len(entities))
            entity_facts = db.execute(f"""SELECT DISTINCT f.id,f.category,f.fact_key,f.fact_value,
                f.trust_score,f.preference_score FROM facts f
                JOIN fact_entities fe ON fe.fact_id=f.id
                JOIN entities e ON e.entity_id=fe.entity_id
                WHERE e.name IN ({placeholders}) AND f.archived=0
                ORDER BY f.trust_score DESC LIMIT 10""", entities).fetchall()
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


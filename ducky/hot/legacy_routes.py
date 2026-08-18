"""ducky.hot.legacy_routes — SQLite Legacy 路由注册"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import datetime as _dt
from typing import Optional
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Form, Query

from ducky.utils import (
    DEFAULT_USER_ID,
    DATA_DIR,
    FACTS_DB,
    OBS_DB,
    SCENES_DB,
)
from ducky.facts_recall import _strict_tenant_enabled, tenant_clause
from ducky.hot.legacy_helpers import (
    _get_facts_conn,
    _get_obs_conn,
    _get_scenes_conn,
    _extract_entities,
    _extract_key_facts,
    _auto_extract_and_link,
    _cluster_scenes_impl,
    _extract_validity,
    TAGS_FILE,
    SKILL_PATTERNS_FILE,
    _pantheon_tier,
    _PANTHEON_DEFAULT_AGENT,
    _PANTHEON_DEFAULT_PROFILE,
    CONTRADICTION_WORDS,
    _auto_detect_level,
    _ensure_observations_table,
    _ensure_scenes_table,
    _observations_columns,
    _fact_feedback_impl,
    _load_tags,
    _run_consolidation,
    _vault_refine,
)

logger = logging.getLogger("aiduMEM.legacy.routes")

def register_legacy_routes(app):
    """把 §6-§10 的全部 22 个端点注册到 FastAPI app 上"""

    # ── 6.2  Facts CRUD ──
    @app.get("/facts")
    def list_facts(category: str = None, key: str = None, level: str = "L2",
                   user_id: str = DEFAULT_USER_ID):
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
        t_clause, t_params = tenant_clause(user_id)
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

    @app.post("/facts/add")
    def add_fact(category: str = "general", fact_key: str = "", fact_value: str = "",
                 source: str = DEFAULT_USER_ID, level: str = "",
                 valid_from: str = "", valid_to: str = "",
                 agent_id: str = ""):
        if not fact_key or not fact_value:
            return {"status":"error","detail":"fact_key 和 fact_value 不能为空"}
        from ducky.security.injection_guard import validate_and_sanitize_memory_content
        is_safe, sanitized_val, rejection = validate_and_sanitize_memory_content(fact_value)
        if not is_safe:
            logger.warning("🛡️ [InjectionGuard] /facts/add 拦截注入: %s", rejection)
            return {"status": "error", "detail": f"Fact value rejected: {rejection}"}
        fact_value = sanitized_val
        resolved_level = level if level else _auto_detect_level(category)
        summary = f"{fact_value[:60]}{'...' if len(fact_value)>60 else ''}"
        overview = fact_value
        # Chronos 双时间轴：显式参数优先，否则保守抽取；抽不出=None（永不过期）
        vf, vt = _extract_validity(f"{fact_key} {fact_value}")
        vf = (valid_from or "").strip() or vf
        vt = (valid_to or "").strip() or vt
        # Pantheon 分层：旧端点也自动分层归属，保证 v12 调用方无需改代码
        # 就能享受铁律零衰减；响应结构只增字段不改语义。
        fed_tier = _pantheon_tier.infer_tier(category, fact_key, fact_value)
        recorded_at = _dt.datetime.now(_dt.timezone.utc)
        decay_at = _pantheon_tier.decay_deadline(fed_tier, recorded_at)
        # 🔴P0-2b（v19.4.1 施工中新发现，比可见性泄漏更严重）：
        #     此前 agent_id 恒写常量 _PANTHEON_DEFAULT_AGENT，而唯一约束是
        #     ON CONFLICT(agent_id, category, fact_key) —— 于是不同租户写同一
        #     (category, fact_key) 会命中同一冲突键，**后写者直接覆盖前者的
        #     fact_value**。实测：alice 写 favorite_drink 后 bob 再写，库里只剩
        #     bob 的值，alice 的事实被静默销毁。这是跨租户数据破坏，不是泄漏。
        #     修复：agent_id 显式可传；未传时回退 source（租户名），
        #     source 也为默认值时才用 _PANTHEON_DEFAULT_AGENT，保证存量部署
        #     （靠 AIDUMEM_DEFAULT_AGENT_ID 统一归属）行为零变化。
        effective_agent = (agent_id or "").strip()
        if not effective_agent:
            _src = (source or "").strip()
            effective_agent = _src if (_src and _src != DEFAULT_USER_ID) else _PANTHEON_DEFAULT_AGENT
        conn = _get_facts_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO facts (category, fact_key, fact_value, source, summary, overview, level,
                               valid_from, valid_to, agent_id, profile, memory_tier,
                               recorded_at, decay_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(agent_id, category, fact_key) DO UPDATE SET
                fact_value=excluded.fact_value, source=excluded.source,
                summary=excluded.summary, overview=excluded.overview,
                level=excluded.level, updated_at=CURRENT_TIMESTAMP,
                memory_tier=excluded.memory_tier,
                recorded_at=excluded.recorded_at,
                decay_at=excluded.decay_at,
                valid_from=COALESCE(excluded.valid_from, facts.valid_from),
                valid_to=COALESCE(excluded.valid_to, facts.valid_to)
        """, (category, fact_key, fact_value, source, summary, overview, resolved_level,
              vf, vt, effective_agent, _PANTHEON_DEFAULT_PROFILE, fed_tier,
              recorded_at.isoformat(), decay_at))
        # 📒 事件账本（v19.4.0 Mímir 借鉴 B5）：与事实写入同事务留痕，同生共死
        try:
            from ducky.event_ledger import content_hash, record_event
            record_event(conn, actor=source or "tool", action="add",
                         target_id=f"fact:{fact_key}", reason=f"category={category}",
                         after_hash=content_hash(fact_value))
        except Exception as le:
            logger.debug("ledger 记录跳过: %s", le)
        fid = cur.lastrowid or 0
        # 🏛️ 治理管线（v19.4.0 Mímir 借鉴 B1）：写入后审计 + provisional 语义。
        #    规则 reject 同事务归档+tombstone 留痕；待审事实降权 0.30。
        gov = {"route": "skipped"}
        try:
            from ducky.governance import govern_fact_write
            gov = govern_fact_write(conn, fid, category, fact_key, fact_value,
                                    user_id=source or DEFAULT_USER_ID)
        except Exception as ge:
            logger.debug("governance 钩子跳过: %s", ge)
        conn.commit()
        # 独立评估器异步补审（commit 后；评估器失败/超时保守进人审，绝不自动批准）
        if gov.get("route") == "llm_eval" and gov.get("candidate_id"):
            try:
                from ducky.governance import spawn_async_eval
                spawn_async_eval(gov["candidate_id"])
            except Exception as ae:
                logger.debug("异步评估派发跳过: %s", ae)
        auto_link = _auto_extract_and_link(fid, fact_value, conn)
        conn.close()
        return {"status":"ok","message":f"事实已存储: {category}/{fact_key}","level":resolved_level,
                "validity":{"valid_from":vf,"valid_to":vt},
                "memory_tier": fed_tier,
                "governance": gov,
                "refinement":_vault_refine(category, fact_key, fact_value, resolved_level),
                "auto_entities": auto_link}

    @app.get("/facts/categories")
    def list_fact_categories(user_id: str = DEFAULT_USER_ID):
        conn = _get_facts_conn()
        # 🔴P0-2：类别计数也按租户收窄，否则可从类别分布反推他人数据规模
        t_clause, t_params = tenant_clause(user_id)
        rows = conn.execute(
            "SELECT category, COUNT(*) AS cnt FROM facts WHERE 1=1" + t_clause
            + " GROUP BY category ORDER BY category",
            t_params,
        ).fetchall()
        conn.close()
        return {"status":"ok","categories":[dict(r) for r in rows]}

    # ── 6.3  实体 API ──
    @app.get("/facts/entities")
    def fact_entities(fact_id: int = None, entity: str = None, limit: int = 20,
                      user_id: str = DEFAULT_USER_ID):
        conn = _get_facts_conn()
        cur = conn.cursor()
        t_clause, t_params = tenant_clause(user_id, alias="f")
        if fact_id:
            # 🔴P0-2：先校验该 fact 是否属于本租户可见范围，再吐它的实体，
            # 否则可用他人 fact_id 探测其实体图谱。
            owned = cur.execute(
                "SELECT 1 FROM facts f WHERE f.id=?" + t_clause,
                [fact_id] + t_params,
            ).fetchone()
            if not owned:
                conn.close()
                return {"status":"ok","fact_id":fact_id,"entities":[],"count":0}
            rows = cur.execute("""
                SELECT e.entity_id, e.name, e.entity_type
                FROM entities e JOIN fact_entities fe ON fe.entity_id=e.entity_id
                WHERE fe.fact_id=? ORDER BY e.name
            """, (fact_id,)).fetchall()
            conn.close()
            return {"status":"ok","fact_id":fact_id,"entities":[dict(r) for r in rows],"count":len(rows)}
        elif entity:
            rows = cur.execute("""
                SELECT f.id, f.category, f.fact_key, f.fact_value, f.trust_score, f.updated_at
                FROM facts f JOIN fact_entities fe ON fe.fact_id=f.id
                JOIN entities e ON e.entity_id=fe.entity_id
                WHERE e.name LIKE ? AND f.archived=0
            """ + t_clause + """
                ORDER BY f.updated_at DESC LIMIT ?
            """, [entity] + t_params + [limit]).fetchall()
            conn.close()
            return {"status":"ok","entity":entity,"facts":[dict(r) for r in rows],"count":len(rows)}
        else:
            conn.close()
            return {"status":"error","detail":"需要 fact_id 或 entity 参数"}

    @app.get("/facts/related")
    def fact_related(entity: str = "", limit: int = 10, user_id: str = DEFAULT_USER_ID):
        if not entity: return {"status":"error","detail":"需要 entity 参数"}
        conn = _get_facts_conn()
        t_clause, t_params = tenant_clause(user_id, alias="f")
        rows = conn.execute("""
            SELECT f.id, f.category, f.fact_key, f.fact_value, f.trust_score, f.updated_at,
                   (SELECT GROUP_CONCAT(DISTINCT e3.name) FROM fact_entities fe3
                    JOIN entities e3 ON e3.entity_id=fe3.entity_id WHERE fe3.fact_id=f.id) as shared_entities
            FROM facts f
            JOIN fact_entities fe ON fe.fact_id = f.id
            JOIN entities e ON e.entity_id = fe.entity_id
            WHERE e.name LIKE ? AND f.archived = 0 AND f.id NOT IN (
                SELECT f2.id FROM facts f2
                JOIN fact_entities fe2 ON fe2.fact_id=f2.id
                JOIN entities e2 ON e2.entity_id=fe2.entity_id
                WHERE e2.name LIKE ?
            )
            """ + t_clause + """
            GROUP BY f.id ORDER BY f.trust_score DESC, COUNT(DISTINCT e.name) DESC LIMIT ?
        """, [entity, entity] + t_params + [limit]).fetchall()
        conn.close()
        return {"status":"ok","entity":entity,"related":[dict(r) for r in rows],"count":len(rows)}

    @app.get("/facts/reason")
    def fact_reason(entities: str = "", limit: int = 10, user_id: str = DEFAULT_USER_ID):
        if not entities: return {"status":"error","detail":"需要 entities 参数（逗号分隔）"}
        e_list = [e.strip() for e in entities.split(",") if e.strip()]
        if len(e_list) < 2: return {"status":"error","detail":"需要至少 2 个实体"}
        conn = _get_facts_conn()
        placeholders = ",".join("?" * len(e_list))
        t_clause, t_params = tenant_clause(user_id, alias="f")
        rows = conn.execute(f"""
            SELECT f.id, f.category, f.fact_key, f.fact_value, f.trust_score, f.updated_at,
                   GROUP_CONCAT(DISTINCT e.name) as matched_entities,
                   COUNT(DISTINCT e.name) as match_count
            FROM facts f
            JOIN fact_entities fe ON fe.fact_id=f.id
            JOIN entities e ON e.entity_id=fe.entity_id
            WHERE e.name IN ({placeholders}) AND f.archived=0 {t_clause}
            GROUP BY f.id
            HAVING COUNT(DISTINCT e.name) >= ?
            ORDER BY f.trust_score DESC, match_count DESC LIMIT ?
        """, e_list + t_params + [len(e_list), limit]).fetchall()
        conn.close()
        return {"status":"ok","query_entities":e_list,"results":[dict(r) for r in rows],
                "count":len(rows),"min_match":len(e_list)}

    @app.get("/facts/entities/list")
    def list_entities(entity_type: str = None, limit: int = 50):
        conn = _get_facts_conn()
        if entity_type:
            rows = conn.execute("""
                SELECT e.*, COUNT(fe.fact_id) as fact_count
                FROM entities e LEFT JOIN fact_entities fe ON fe.entity_id=e.entity_id
                WHERE e.entity_type=? GROUP BY e.entity_id ORDER BY fact_count DESC LIMIT ?
            """, (entity_type, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT e.*, COUNT(fe.fact_id) as fact_count
                FROM entities e LEFT JOIN fact_entities fe ON fe.entity_id=e.entity_id
                GROUP BY e.entity_id ORDER BY fact_count DESC LIMIT ?
            """, (limit,)).fetchall()
        conn.close()
        return {"status":"ok","entities":[dict(r) for r in rows],"count":len(rows)}

    # ── 6.4  矛盾检测 v2 ──
    @app.post("/prune/contradiction-v2")
    def detect_contradictions_v2(dry_run: bool = True, min_overlap: float = 0.3, limit: int = 20):
        conn = _get_facts_conn()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id,category,fact_key,fact_value,trust_score FROM facts WHERE archived=0 ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
        if len(rows) < 2:
            conn.close()
            return {"status":"ok","contradictions":[],"count":0}
        by_cat = defaultdict(list)
        for r in rows: by_cat[r["category"]].append(dict(r))
        all_ids = [r["id"] for r in rows]
        entity_rows = cur.execute(f"""
            SELECT fe.fact_id, e.name FROM fact_entities fe
            JOIN entities e ON e.entity_id=fe.entity_id
            WHERE fe.fact_id IN ({','.join('?'*len(all_ids))})
        """, all_ids).fetchall()
        fact_ents = defaultdict(set)
        for fid, ename in entity_rows: fact_ents[fid].add(ename.lower())

        def jaccard_tokens(t1, t2):
            if not t1 or not t2: return 0.0
            s1 = set(re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]{2,}', (t1 or "").lower()))
            s2 = set(re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]{2,}', (t2 or "").lower()))
            return len(s1&s2)/len(s1|s2) if (s1|s2) else 0.0

        contradictions = []
        for cat, cat_rows in by_cat.items():
            n = min(len(cat_rows), 50)
            for i in range(n):
                for j in range(i+1, n):
                    f1, f2 = cat_rows[i], cat_rows[j]
                    ents1, ents2 = fact_ents.get(f1["id"],set()), fact_ents.get(f2["id"],set())
                    if not ents1 or not ents2: continue
                    e_overlap = len(ents1&ents2)/len(ents1|ents2) if (ents1|ents2) else 0.0
                    if e_overlap < min_overlap: continue
                    c_sim = jaccard_tokens(f1["fact_value"], f2["fact_value"])
                    c_score = e_overlap * (1.0 - c_sim)
                    if c_score >= 0.15:
                        ps = sorted([
                            {"id":f1["id"],"key":f1["fact_key"],"value":(f1["fact_value"]or"")[:200],"trust":f1["trust_score"]},
                            {"id":f2["id"],"key":f2["fact_key"],"value":(f2["fact_value"]or"")[:200],"trust":f2["trust_score"]}
                        ], key=lambda x: x["trust"], reverse=True)
                        contradictions.append({"category":cat,"higher_trust":ps[0],"lower_trust":ps[1],
                            "entity_overlap":round(e_overlap,3),"content_similarity":round(c_sim,3),
                            "contradiction_score":round(c_score,3)})
                        if not dry_run and len(contradictions)<=limit:
                            cur.execute("UPDATE facts SET trust_score=MAX(0.1,trust_score*0.5),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                       (ps[1]["id"],))
        if not dry_run: conn.commit()
        contradictions.sort(key=lambda x: x["contradiction_score"], reverse=True)
        result = contradictions[:limit]
        conn.close()
        total_pairs = sum(min(len(v),50)*(min(len(v),50)-1)//2 for v in by_cat.values() if len(v)>=2)
        return {"status":"ok (dry-run)" if dry_run else "ok (verified)","pairs_scanned":total_pairs,
                "contradictions_found":len(result),"contradictions":result}

    # ── 6.5  Feedback ──
    @app.post("/facts/feedback")
    def fact_feedback(fact_id: int, helpful: bool):
        return _fact_feedback_impl(fact_id, helpful)

    # ── 6.6  旧 v1 矛盾检测 ──
    @app.post("/prune/contradiction")
    def detect_contradictions(dry_run: bool = True, min_trust: float = 0.3):
        conn = _get_facts_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT category, COUNT(*) as cnt, GROUP_CONCAT(id) as ids
            FROM (SELECT id,category,trust_score FROM facts WHERE archived=0 AND trust_score>=?
                  ORDER BY id DESC LIMIT 500)
            GROUP BY category HAVING COUNT(*)>=2 ORDER BY cnt DESC LIMIT 10
        """, (min_trust,))
        groups = cur.fetchall()
        contradictions, audited = [], 0
        for cat, cnt, ids_str in groups:
            ids = [int(x) for x in ids_str.split(",")][:50]
            cur.execute(f"SELECT id,fact_key,fact_value,trust_score FROM facts WHERE id IN ({','.join('?'*len(ids))})", ids)
            rows = cur.fetchall()
            if len(rows)<2: continue
            for i in range(len(rows)):
                for j in range(i+1, len(rows)):
                    v1, v2 = rows[i]["fact_value"], rows[j]["fact_value"]
                    if not v1 or not v2: continue
                    for word_set in CONTRADICTION_WORDS:
                        if (word_set[0] in v1 or word_set[0] in v2) and any(w in v1 or w in v2 for w in word_set[1:]):
                            low = rows[i] if rows[i]["trust_score"]<=rows[j]["trust_score"] else rows[j]
                            high = rows[j] if rows[i]["trust_score"]<=rows[j]["trust_score"] else rows[i]
                            contradictions.append({"category":cat,"fact_key":rows[i]["fact_key"][:30],
                                "lower_trust":{"id":low["id"],"trust":low["trust_score"],"value":(low["fact_value"]or"")[:100]},
                                "higher_trust":{"id":high["id"],"trust":high["trust_score"],"value":(high["fact_value"]or"")[:100]},
                                "pattern":word_set[0]})
                            if not dry_run:
                                cur.execute("UPDATE facts SET trust_score=MAX(0.1,trust_score*0.5),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                           (low["id"],)); audited += 1
                            break
        if not dry_run: conn.commit()
        conn.close()
        return {"status":"ok (dry-run)" if dry_run else "ok","groups_scanned":len(groups),
                "contradictions_found":len(contradictions),"audited":audited,"contradictions":contradictions[:10]}

    # ── 6.7  标签系统 + 信任统计 ──
    @app.post("/facts/tags/generate")
    def generate_tags(fact_id: int | None = None):
        return {"status":"ok","tags":[],"note":"auto-tag generation v2"}

    @app.get("/facts/tags")
    def get_tags(fact_id: int | None = None):
        return {"status":"ok","tags":_load_tags()}

    @app.get("/facts/trust-stats")
    def fact_trust_stats(user_id: str = DEFAULT_USER_ID):
        conn = _get_facts_conn()
        t_clause, t_params = tenant_clause(user_id)
        rows = conn.execute("""
            SELECT category,
                   COUNT(*) as cnt, AVG(trust_score) as avg_trust,
                   SUM(helpful_count) as helpful, SUM(unhelpful_count) as unhelpful
            FROM facts WHERE archived=0
        """ + t_clause + """ GROUP BY category ORDER BY cnt DESC
        """, t_params).fetchall()
        conn.close()
        return {"status":"ok","categories":[dict(r) for r in rows]}

    # ── §7  搜索 ──
    @app.get("/facts/search")
    def search_facts(query: str = "", category: str = None, top_k: int = 10,
                     min_trust: float = 0.0, use_hybrid: bool = True,
                     level: str = "L2", before: str = "", after: str = "",
                     user_id: str = DEFAULT_USER_ID):
        # facts 是独立结构化知识库，不再绕经 mem0/Qdrant；use_hybrid 保留为兼容参数。
        # P0-1 时间过滤：before/after 支持 YYYY[-MM[-DD]] 粒度。
        from ducky.facts_recall import search_facts as recall_facts
        return recall_facts(
            query,
            category=category,
            top_k=top_k,
            level=level,
            min_trust=min_trust,
            before=before,
            after=after,
            user_id=user_id,
        )

    # ── §8  Observations + Reflect ──
    @app.post("/observe/consolidate")
    def run_consolidation(user_id: str = DEFAULT_USER_ID):
        return _run_consolidation(user_id)

    @app.get("/observe")
    def list_observations(category: str = None, limit: int = 20, include_stale: bool = False,
                          user_id: str = DEFAULT_USER_ID):
        conn = _get_obs_conn()
        # 🔴P1-3（v19.4.1）：observations 表此前全仓无 DDL，全新部署调
        # /observe 直接 no such table 500。此处幂等建表，开箱返回空集。
        _ensure_observations_table(conn)
        where = "WHERE 1=1"
        params = []
        if category: where+=" AND category=?"; params.append(category)
        if not include_stale: where+=" AND is_stale=0"
        # 🔴P0-2：按 user_id 收窄可见范围。
        # 但**必须先确认列存在**：生产库的 observations 是 v7 时代手工建的，
        # 没有 user_id 列。若无条件拼进 WHERE，实机会直接
        # `no such column: user_id` 500 —— 这正是本地测试库与生产库
        # schema 分叉能造成的伤害（v19.4.1 施工中在实机 schema 探针下发现）。
        # 迁移会补列，但补列可能因权限/锁失败，读取路径不能依赖它成功。
        _uid = (user_id or "").strip()
        if _uid and _uid != DEFAULT_USER_ID and "user_id" in _observations_columns(conn):
            if _strict_tenant_enabled():
                where += " AND user_id=?"; params.append(_uid)
            else:
                # 空 user_id 的历史行视为未标记归属，宽松档下对本机可见
                where += " AND (user_id=? OR user_id='' OR user_id IS NULL)"; params.append(_uid)
        rows = conn.execute(f"SELECT * FROM observations {where} ORDER BY updated_at DESC LIMIT ?", params+[limit]).fetchall()
        conn.close()
        return {"status":"ok","observations":[dict(r) for r in rows],"count":len(rows)}

    # 注：v19.0 起 /reflect 端点由 ducky.routes_p0 提供真正的 LLM 反思引擎。
    # 这里只保留旧的「关联记忆检索」helper，供 /observe/related 继续使用。
    def _legacy_related_search(question: str, top_k: int = 10, use_llm: bool = True):
        try:
            from api_server import get_memory
            mem = get_memory()
            results = mem.search(question, filters={"user_id": DEFAULT_USER_ID}, limit=top_k)
            if isinstance(results, dict):
                results = results.get("results", [])
            if not isinstance(results, list):
                results = []
            return {"status":"ok","question":question,"results":results[:top_k]}
        except Exception as e:
            return {"status":"error","detail":str(e)}

    @app.get("/observe/related")
    def get_related(query: str, top_k: int = 5):
        return _legacy_related_search(query, top_k)

    # ── §9  Scene 聚类 + Persona ──
    @app.post("/scene/cluster")
    def cluster_scenes(category: str = None, dry_run: bool = True, min_similarity: float = 0.25):
        return _cluster_scenes_impl(category, dry_run, min_similarity)

    @app.get("/scene")
    def list_scenes(category: str = None, limit: int = 20):
        conn = _get_scenes_conn()
        _ensure_scenes_table(conn)  # 🔴6：保证表存在，避免开箱 500
        if category:
            rows = conn.execute(
                "SELECT * FROM scenes WHERE category=? ORDER BY created_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM scenes ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        conn.close()
        return {"status":"ok","scenes":[dict(r) for r in rows]}

    def _refresh_persona_inline(name: str = "user"):
        conn = _get_facts_conn()
        rows = conn.execute("""
            SELECT fact_key, fact_value FROM facts
            WHERE archived=0 AND (category LIKE '%项目%' OR category LIKE '%AI%' OR fact_key LIKE '%user%')
            ORDER BY trust_score DESC LIMIT 100
        """).fetchall()
        conn.close()
        return {"status":"ok","name":name,"facts_count":len(rows)}

    @app.get("/persona")
    def get_persona(name: str = "user"):
        return _refresh_persona_inline(name)

    # 注：/persona/build 已让位给 v19.0 人格记忆基座（ducky.routes_persona）。
    # 旧「AI 自我人设刷新」逻辑保留为 /persona/refresh，避免路径冲突。
    @app.post("/persona/refresh")
    def build_persona(name: str = Form("user")):
        return _refresh_persona_inline(name)

    # ── §10  Skill 发现 ──
    @app.post("/skill/discover")
    def discover_skill_patterns(dry_run: bool = True):
        conn = _get_facts_conn()
        rows = conn.execute("""
            SELECT fact_key, fact_value, COUNT(*) as cnt
            FROM facts WHERE category='Solution' AND archived=0
            GROUP BY fact_key HAVING COUNT(*)>=3 ORDER BY cnt DESC LIMIT 20
        """).fetchall()
        conn.close()
        discovered = []
        for r in rows:
            content = r["fact_value"]
            steps = [kw for kw in ["升级","配置","登录","重启","删除","复制","备份","curl","扫描","验证"] if kw in content]
            if steps: discovered.append({"key":r["fact_key"],"repeat":r["cnt"],"steps":steps})
        return {"status":"ok (dry-run)" if dry_run else "ok","discovered":len(discovered),"patterns":discovered}

    logger.info(f"✅ 22 legacy 端点已注册到 FastAPI app")

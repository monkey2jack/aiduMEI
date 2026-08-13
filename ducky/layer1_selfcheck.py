#!/usr/bin/env python3
"""
aiduMEM Layer 1: 写入自检模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Aion Memory 设计哲学：
- 每次写入时自检，不依赖外部 cron
- 容量 >80% → 自动合并同域记忆
- 重复 → 更新而非新增
- Instinct 同域 ≥3 → 标记可毕业
"""

import json, logging, time
from typing import Optional

from .utils import get_facts_conn, jaccard_sim

logger = logging.getLogger("aiduMEM.selfcheck")

# ── 配置 ──
MAX_CAPACITY = 1000           # 单用户最大记忆数
CAPACITY_THRESHOLD = 0.80     # 触发合并的容量阈值
DEDUP_THRESHOLD = 0.85        # 去重相似度阈值
MERGE_MIN_GROUP = 3           # 合并最少同组条数


def check_capacity(memory, user_id: str) -> dict:
    """检查容量，返回 {total, pct, needs_merge}"""
    try:
        all_mem = memory.get_all(filters={"user_id": user_id}, limit=10000)
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        total = len(results) if isinstance(results, list) else 0
        pct = total / MAX_CAPACITY if MAX_CAPACITY > 0 else 0
        return {
            "total": total,
            "max": MAX_CAPACITY,
            "pct": round(pct, 3),
            "needs_merge": pct >= CAPACITY_THRESHOLD,
        }
    except Exception as e:
        logger.warning(f"容量检查失败: {e}")
        return {"total": 0, "max": MAX_CAPACITY, "pct": 0, "needs_merge": False}


def dedup_check(memory, user_id: str, new_text: str) -> Optional[str]:
    """检查是否已存在相似记忆，返回已有 memory_id 或 None"""
    try:
        results = memory.search(new_text, filters={"user_id": user_id}, limit=3)
        if not results:
            return None
        results_list = results.get("results", results) if isinstance(results, dict) else results
        if not results_list:
            return None
        # mem0 search 返回的是按相似度排序的，第一条最相似
        top = results_list[0]
        # mem0 的 search 结果中 score 通常是距离，越小越相似
        # Qdrant 的 score 需要转换：score > 0.7 即相似
        score = top.get("score", 0) if isinstance(top, dict) else 0
        # 如果 score 较高（mem0 返回的距离越小越相似，但有些版本返回相似度）
        # 我们同时检查文本相似度
        existing_text = top.get("memory", "") if isinstance(top, dict) else ""
        if existing_text and _text_similarity(new_text[:200], existing_text[:200]) > DEDUP_THRESHOLD:
            return top.get("id", "")
    except Exception as e:
        logger.debug(f"去重检查跳过: {e}")
    return None


def _text_similarity(a: str, b: str) -> float:
    """简单的 Jaccard 相似度（字符级 bigram）"""
    if not a or not b:
        return 0.0
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1))
    ba, bb = bigrams(a), bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def auto_merge_similar(memory, user_id: str, max_groups: int = 5) -> dict:
    """合并同类记忆：同 metadata.source 或 category 的 ≥3 条 → 保留最新"""
    try:
        all_mem = memory.get_all(filters={"user_id": user_id}, limit=10000)
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        if not isinstance(results, list) or len(results) < MERGE_MIN_GROUP:
            return {"merged_groups": 0, "deleted": 0}

        # 按 metadata.source 分组
        groups = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            source = meta.get("source", "unknown")
            if source not in groups:
                groups[source] = []
            groups[source].append(item)

        merged = 0
        deleted_total = 0
        for source, items in list(groups.items())[:max_groups]:
            if len(items) < MERGE_MIN_GROUP:
                continue
            # 保留最新的一条，删除其余
            items_sorted = sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)
            for old_item in items_sorted[1:]:
                try:
                    memory.delete(old_item["id"])
                    deleted_total += 1
                except Exception as e:
                    logger.debug(f"删除记忆 {old_item.get('id','')[:8]} 失败: {e}")
            merged += 1

        logger.info(f"Layer1 自动合并: {merged} 组, 删除 {deleted_total} 条")
        return {"merged_groups": merged, "deleted": deleted_total}
    except Exception as e:
        logger.warning(f"自动合并失败: {e}")
        return {"merged_groups": 0, "deleted": 0}


def layer1_add_wrapper(memory, messages_json, user_id: str, metadata: dict) -> dict:
    """
    Layer 1 写入包装器：
    1. 去重检查
    2. 容量检查 → 需要时自动合并
    3. 写入记忆
    """
    start = time.time()
    action = "new"
    details = {}

    # 提取文本用于去重
    text = ""
    if isinstance(messages_json, list):
        text = " ".join(m.get("content", "") for m in messages_json if isinstance(m, dict))
    elif isinstance(messages_json, dict):
        text = messages_json.get("content", str(messages_json))
    else:
        text = str(messages_json)

    # Step 0: P0-2 记忆去重自编辑（LLM 语义级判重，先行；失败降级回 Jaccard）
    try:
        from ducky.self_edit import self_edit_on_add
        self_edit_result = self_edit_on_add(memory, user_id, messages_json, metadata)
        if self_edit_result:
            details["self_edit"] = self_edit_result
            action = self_edit_result["action"]
            # self-edit 直接更新了既有记忆内容，记忆向量与文本索引会因
            # update 而异动；热度与 FTS 仍需同步，否则合并后的记忆在
            # 检索侧被降权/漏检。这里做保守同步，失败不阻断返回。
            _sync_indexes_after_update(
                memory,
                memory_id=self_edit_result.get("memory_id", ""),
                content=self_edit_result.get("merged_content", text),
                user_id=user_id,
            )
            elapsed_ms = int((time.time() - start) * 1000)
            details["ms"] = elapsed_ms
            return {
                "status": "ok",
                "action": action,
                "details": details,
            }
    except Exception as se:
        logger.debug(f"self-edit 跳过（降级）: {se}")

    # Step 1: 去重检查
    existing_id = dedup_check(memory, user_id, text)
    if existing_id:
        try:
            # Lethe v9.2.0: 触发演化追踪 (在更新前运行，便于捕获相似关系)
            track_knowledge_evolution(memory, user_id, text, existing_id)
            memory.update(existing_id, text, metadata=metadata)
            action = "updated"
            details["existing_id"] = existing_id
            logger.info(f"Layer1 去重更新: {existing_id[:16]}")
        except Exception:
            # update 失败就走新增
            memory.add(messages_json, user_id=user_id, metadata=metadata)
            action = "new"
    else:
        # Step 2: 容量检查
        cap = check_capacity(memory, user_id)
        details["capacity"] = cap
        if cap["needs_merge"]:
            merge_result = auto_merge_similar(memory, user_id)
            details["merge"] = merge_result
            action = "merged" if merge_result["merged_groups"] > 0 else "new"

        # Lethe v9.2.0: 写入前进行演化追踪，将可能被新记忆取代的旧记忆置为 superseded
        import hashlib
        try:
            new_id_placeholder = hashlib.md5(text.encode()).hexdigest()
            track_knowledge_evolution(memory, user_id, text, new_id_placeholder)
        except Exception as e:
            logger.warning(f"写入前演化追踪失败: {e}")

        # Step 3: 写入
        memory.add(messages_json, user_id=user_id, metadata=metadata)

    elapsed_ms = int((time.time() - start) * 1000)
    details["ms"] = elapsed_ms

    return {
        "status": "ok",
        "action": action,
        "details": details,
    }


def _sync_indexes_after_update(memory, memory_id: str, content: str, user_id: str) -> None:
    """self-edit 合并/冲突更新记忆后，补做热度登记与 FTS 索引刷新。

    与 /add 正常写入路径保持一致；任何一步失败都静默降级，不阻断
    self-edit 的返回（记忆内容本身已经更新成功）。
    """
    if not memory_id:
        return
    try:
        # 合并是「更新」不是「新增」：走 preserve_heat=True 保留既有热度，
        # 避免 register_salience_for_add 的 INSERT OR REPLACE 把 access_count
        # 清零、把高频访问的旧记忆降权。
        from ducky.salience.core import on_memory_added
        on_memory_added(memory_id, content=content, preserve_heat=True)
    except Exception as e:
        logger.debug(f"self-edit 热度登记跳过: {e}")
    try:
        from ducky.text_fts import _index_memory
        _index_memory(memory_id, content, user_id=user_id, category="")
    except Exception as e:
        logger.debug(f"self-edit FTS 索引刷新跳过: {e}")


def track_knowledge_evolution(memory, user_id: str, new_text: str, new_id: str = "new_item"):
    """Lethe v9.2.0: 知识演化追踪 + 状态机流转"""
    try:
        # 1. 查找最相似的候选记忆 (避开新写入的这一条)
        results = memory.search(new_text, filters={"user_id": user_id}, limit=5)
        results_list = results.get("results", results) if isinstance(results, dict) else results
        if not results_list:
            return
        
        for top in results_list:
            old_text = top.get("memory", "")
            old_id = top.get("id", "")
            if not old_text or not old_id or old_id == new_id:
                continue
            
            # 2. 算 Jaccard 相似度 (Lethe v9.2.0: 中文 bigram 级 Jaccard 相似度阈值 + 共同名词检测)
            sim = jaccard_sim(new_text, old_text)
            
            # 中文特化共同话题检测 (如 "围棋", "羽毛球", "拿铁")
            has_common_topic = False
            import re
            cn_new = set(re.findall(r'[\u4e00-\u9fff]{2,}', new_text))
            cn_old = set(re.findall(r'[\u4e00-\u9fff]{2,}', old_text))
            stop_topics = {"user", "AI", "现在", "改为", "喜欢", "不再", "决定", "已经", "改为", "为了"}
            common_topics = (cn_new & cn_old) - stop_topics
            if common_topics:
                has_common_topic = True
                
            if sim < 0.12 and not has_common_topic:
                continue
                
            # 3. 判定关系类型
            relation = "enriches"
            reason = f"jaccard_sim={sim:.2f}"
            
            replaces_keywords = ["改为", "取代", "更新为", "不用了", "废弃", "修改为", "修正为", "现在是", "而不是"]
            negation_keywords = ["不", "否", "非", "no", "not"]
            
            has_replaces = any(kw in new_text for kw in replaces_keywords)
            
            text_a, text_b = old_text.lower(), new_text.lower()
            contradict_pos = ["use", "choose", "select", "recommend", "best", "optimal", "采用", "使用", "推荐"]
            contradict_neg = ["avoid", "not", "never", "wrong", "deprecated", "不要", "不应", "避免"]
            a_pos = any(w in text_a for w in contradict_pos)
            b_neg = any(w in text_b for w in contradict_neg)
            a_neg = any(w in text_a for w in contradict_neg)
            b_pos = any(w in text_b for w in contradict_pos)
            
            is_polar_flip = (a_pos and b_neg) or (a_neg and b_pos)
            
            if has_replaces or is_polar_flip:
                relation = "replaces"
                
            # 4. 保存演化关系到 facts.db
            conn = get_facts_conn()
            conn.execute(
                "INSERT INTO knowledge_evolution (source_id, target_id, relation_type, confidence, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (old_id, new_id, relation, sim, reason)
            )
            
            # 5. 如果是 replaces，将旧记忆的状态标记为 superseded
            if relation == "replaces":
                conn.execute(
                    "INSERT OR REPLACE INTO memory_states (memory_id, state, reason, source) VALUES (?, 'superseded', ?, 'evolution')",
                    (old_id, f"replaced_by:{new_id}")
                )
                conn.execute(
                    "INSERT OR REPLACE INTO memory_states (memory_id, state, reason, source) VALUES (?, 'active', 'new_evolution_active', 'evolution')",
                    (new_id,)
                )
            conn.commit()
            conn.close()
            logger.info(f"Lethe 演化追踪: {old_id[:8]} -[{relation}]-> {new_id[:8]} (sim={sim:.2f})")
    except Exception as e:
        logger.warning(f"演化追踪失败: {e}")

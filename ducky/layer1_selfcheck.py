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

import logging, math, os, time
from typing import Optional

from .bank_contract import (
    DEFAULT_BANK_ID,
    stamp_bank_metadata,
    vector_item_in_bank,
    vector_scope_filters,
)
from .utils import get_facts_conn, jaccard_sim
from ducky.failure_ledger import feature_failed

logger = logging.getLogger("aiduMEM.selfcheck")

# ── 配置 ──
# 🔴f0.1+：这三个曾是写死的模块常量，用户踩到误删后**没有任何配置手段自救**，
# 只能改源码。现在全部可由环境变量覆盖，并登记进 ducky/env_registry.py。
def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.environ.get(name, "")).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """读浮点配置；非有限值（NaN/inf）一律回落默认。

    NaN 会让 ``pct >= CAPACITY_THRESHOLD`` 恒为 False（看着像「安全」），
    也会让相似度判据恒为 False —— 两种都是静默失效，所以先拦掉。
    """
    try:
        v = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v) or v <= 0:
        return default
    return v


MAX_CAPACITY = _env_int("AIDUMEI_MAX_CAPACITY", 1000)              # 单用户最大记忆数
CAPACITY_THRESHOLD = _env_float("AIDUMEI_CAPACITY_THRESHOLD", 0.80)  # 触发合并的容量阈值
DEDUP_THRESHOLD = 0.85        # 去重相似度阈值
MERGE_MIN_GROUP = 3           # 合并最少同组条数

# 🔴f0.1+ P0：自动合并**默认关闭**。
#
# 起因是外部用户实锤：auto_merge_similar 名为「合并相似记忆」，实现却只按
# metadata.source 分组、完全不比内容，把几百条话题各异的记忆当成「同类」，
# 只留最新一条，其余直接 memory.delete() 真删（单次删掉 794~864 条）。
#
# 相似度判据已在本版修好（见 auto_merge_similar），但**默认仍然关闭**：
# 宁可让库涨到上限、如实告警，也不能替用户静默删数据。
# 想要自动合并的人显式开 AIDUMEI_AUTO_MERGE=on。
AUTO_MERGE_ENABLED_DEFAULT = False


def auto_merge_enabled() -> bool:
    """自动合并开关（默认关）。每次读环境变量，便于测试与热调整。"""
    raw = str(os.environ.get("AIDUMEI_AUTO_MERGE", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return AUTO_MERGE_ENABLED_DEFAULT


def check_capacity(memory, user_id: str, bank_id: str = DEFAULT_BANK_ID) -> dict:
    """检查容量，返回 {total, pct, needs_merge}

    🔴v20：容量按**域**计量。此前跨域统计，导致一个域写满会去触发另一个域的
    合并删除（见 ``auto_merge_similar``）。改成按域后，单域部署（全部记忆都在
    default）的行为与 v19 逐字节一致，多域部署则各算各的。
    """
    try:
        all_mem = memory.get_all(filters=vector_scope_filters(user_id, bank_id), limit=10000)
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        results = [r for r in results if vector_item_in_bank(r, bank_id)] \
            if isinstance(results, list) else results
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


def dedup_check(memory, user_id: str, new_text: str,
                bank_id: str = DEFAULT_BANK_ID) -> Optional[str]:
    """检查是否已存在相似记忆，返回已有 memory_id 或 None

    🔴v20：此前只按 ``{"user_id": …}`` 过滤，**跨域命中**。调用方拿到别的域
    的 memory_id 后会 ``memory.update(existing_id, text, metadata=…)`` ——
    那条记忆的正文被改写、bank_id 被改盖成写入方的域：源域凭空少一条，目标
    域多出一条本不属于它的记忆，两个域同时被破坏，且全程无异常无日志。

    过滤沿用向量侧的两半契约：默认域不下推（否则 v19 存量点全被 must 语义
    滤掉），命名域下推；两种情况都再做一次 Python 复筛。
    """
    try:
        filters = vector_scope_filters(user_id, bank_id)
        results = memory.search(new_text, filters=filters, limit=3)
        if not results:
            return None
        results_list = results.get("results", results) if isinstance(results, dict) else results
        if not isinstance(results_list, list):
            return None
        # 复筛掉别的域的候选，再取剩下里最相似的一条
        results_list = [r for r in results_list if vector_item_in_bank(r, bank_id)]
        if not results_list:
            return None
        # mem0 search 返回的是按相似度排序的，第一条最相似
        top = results_list[0]
        # mem0 各版本 score 口径不一（距离/相似度混用），判据统一走文本相似度
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


def _memory_text(item: dict) -> str:
    """取一条记忆的正文（mem0 的字段名历版有别，逐个兜底）。"""
    if not isinstance(item, dict):
        return ""
    for key in ("memory", "text", "content", "data"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _cluster_by_similarity(items: list, threshold: float) -> list:
    """把一组记忆按**内容相似度**聚成簇，返回 [[item, ...], ...]。

    🔴f0.1+ 这是本次整改的核心。此前的实现按 metadata.source 一刀切分组，
    等于「只要来源相同就算同一条记忆」——日常对话沉淀的记忆 source 清一色是
    hermes_turn，于是几百条话题各异的记忆被判为同类，只留最新一条，其余真删。

    注意：把分组键从 source 换成 category **并不能解决问题**。用户可能有
    50 条互不相同的 PREFERENCES，它们 category 相同、内容毫无关系，按标签
    分组照样会被删到只剩一条。**只有比内容才是对的判据。**

    贪心单链聚类：逐条与已有簇的**首条**比对，超过阈值则入簇，否则自成一簇。
    只与首条比（而非簇内全比）是有意为之——保证「留下来的那条」与被删的每
    一条都直接相似，不会因为链式传递把 A~B、B~C 但 A≁C 的三条并成一簇。
    """
    clusters: list = []
    for item in items:
        text = _memory_text(item)
        if not text:
            # 取不到正文就**单独成簇**，永远不会被当成谁的重复删掉
            clusters.append([item])
            continue
        placed = False
        for cluster in clusters:
            head_text = _memory_text(cluster[0])
            if head_text and _text_similarity(text[:200], head_text[:200]) > threshold:
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
    return clusters


def auto_merge_similar(memory, user_id: str, max_groups: int = 5,
                       bank_id: str = DEFAULT_BANK_ID) -> dict:
    """合并**内容真正相似**的记忆：相似度 > DEDUP_THRESHOLD 的 ≥3 条 → 保留最新。

    🔴f0.1+（外部用户实锤整改）：本函数此前名为「合并相似」，实现却
    **一次相似度计算都没有** —— 只按 ``metadata.source`` 分组，把同来源的
    几百条不同话题记忆当成同类，只留最新一条，其余 ``memory.delete()`` 真删。
    用户环境单次删除 794~864 条。同文件第一个相似度函数 ``_text_similarity``
    早就存在、去重路径也一直在用，唯独这里没调。

    本版三处改动：
    1. **真比内容**：先按 source 粗分桶（省比对量），桶内再按
       ``_text_similarity > DEDUP_THRESHOLD`` 聚类，只有真重复才进同一簇；
    2. **默认不删**：``AIDUMEI_AUTO_MERGE`` 默认 off，达阈值也只告警；
    3. **删除可恢复**：走 ``cascade_delete_memory`` 留 tombstone 快照，
       不再直接调 mem0 原生 ``memory.delete()``（那条路绕过了快照）。

    🔴v20：这个函数会**真删**记忆。此前它按 ``{"user_id": …}`` 全域取数，
    往 home 域写一条触发容量合并，能把 work 域里同 source 的旧记忆永久删掉。
    域隔离在这里不是可见性问题，是数据安全问题，所以取数和复筛都必须限定在
    写入方所在的域内。
    """
    try:
        # ── 闸门一：默认关闭 ──────────────────────────────────
        if not auto_merge_enabled():
            logger.info(
                "Layer1 容量达阈值，但自动合并未开启（AIDUMEI_AUTO_MERGE=off，默认）："
                "本次不删除任何记忆。如需自动合并请显式开启。"
            )
            return {"merged_groups": 0, "deleted": 0, "skipped_reason": "auto_merge_disabled"}

        all_mem = memory.get_all(filters=vector_scope_filters(user_id, bank_id), limit=10000)
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        if not isinstance(results, list):
            return {"merged_groups": 0, "deleted": 0}
        # 删除前的最后一道闸：把不属于本域的候选剔干净
        results = [r for r in results if vector_item_in_bank(r, bank_id)]
        if len(results) < MERGE_MIN_GROUP:
            return {"merged_groups": 0, "deleted": 0}

        # ── 第一步：按 source 粗分桶（只为减少两两比对量，**不作为删除判据**）──
        buckets: dict = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            source = meta.get("source", "unknown")
            buckets.setdefault(source, []).append(item)

        merged = 0
        deleted_total = 0
        for _source, bucket in buckets.items():
            if len(bucket) < MERGE_MIN_GROUP:
                continue
            # ── 第二步：桶内按**内容相似度**聚类，这才是删除的唯一判据 ──
            for cluster in _cluster_by_similarity(bucket, DEDUP_THRESHOLD):
                if len(cluster) < MERGE_MIN_GROUP:
                    continue            # 不够 3 条真重复，一条都不删
                if merged >= max_groups:
                    break
                # 保留最新的一条，删除其余
                items_sorted = sorted(cluster, key=lambda x: x.get("created_at", ""), reverse=True)
                for old_item in items_sorted[1:]:
                    mid = old_item.get("id")
                    if not mid:
                        continue
                    # 🔴f0.1+：删除前先留 tombstone 快照，再走级联删除。
                    # 旧实现直接调 mem0 原生 memory.delete()，绕过了快照 ——
                    # 既删错了、又删得找不回来。这里两步都要，且**顺序不能反**。
                    try:
                        from ducky.tombstone import snapshot_before_delete
                        snapshot_before_delete(
                            mid, user_id=user_id, bank_id=bank_id,
                            reason="layer1_auto_merge_capacity",
                            actor="layer1_auto_merge",
                        )
                    except (OSError, ValueError, TypeError, KeyError, ImportError,
                            AttributeError, RuntimeError) as te:
                        # 快照失败不阻断删除主链路（与 wal_engine 同口径），
                        # 但必须留痕 —— 这条记忆将不可恢复。
                        # 收窄到具体类型而非 except Exception：宽捕获棘轮只降不升。
                        logger.warning(f"tombstone 快照失败，{str(mid)[:8]} 删除后不可恢复: {te}")
                    try:
                        from ducky.wal_engine import cascade_delete_memory
                        cascade_delete_memory(mid, user_id=user_id, bank_id=bank_id)
                        deleted_total += 1
                    except Exception as e:
                        logger.debug(f"删除记忆 {str(mid)[:8]} 失败: {e}")
                merged += 1
            if merged >= max_groups:
                break

        logger.info(f"Layer1 自动合并: {merged} 组, 删除 {deleted_total} 条（判据=内容相似度>{DEDUP_THRESHOLD}）")
        return {"merged_groups": merged, "deleted": deleted_total}
    except Exception as e:
        logger.warning(f"自动合并失败: {e}")
        return {"merged_groups": 0, "deleted": 0}


def layer1_add_wrapper(memory, messages_json, user_id: str, metadata: dict, bank_id: str = "default",
                       infer: bool = True) -> dict:
    """
    Layer 1 写入包装器：
    1. 去重检查
    2. 容量检查 → 需要时自动合并
    3. 写入记忆

    ``infer``（v20 新增，默认 True＝生产语义不变）：
    False 时走**免抽取确定性通路** —— 跳过 LLM 语义 self-edit，
    ``memory.add(..., infer=False)`` 直写规范化原文。留在链上的
    去重/容量/演化追踪都只用嵌入检索与规则，同输入必得同输出。
    这是给「跑分器自身可复现」用的（PROTOCOL.md G3b）；正式跑分
    的成绩运行一律 infer=True。
    """
    start = time.time()
    action = "new"
    details: dict = {"infer": bool(infer)}

    # 🔴v20：把域盖进 mem0 metadata —— 这是向量 payload 里唯一能承载 bank_id
    # 的通道（mem0.add 只认 messages/user_id/metadata）。不盖这个戳，命名域的
    # 向量与默认域的向量在 payload 上无法区分，向量侧的域隔离就等于不存在。
    # 在函数口上盖一次，下面 update/add 三个出口全部继承。
    metadata = stamp_bank_metadata(metadata, bank_id)

    # 提取文本用于去重
    text = ""
    if isinstance(messages_json, list):
        text = " ".join(m.get("content", "") for m in messages_json if isinstance(m, dict))
    elif isinstance(messages_json, dict):
        text = messages_json.get("content", str(messages_json))
    else:
        text = str(messages_json)

    # Step 0: P0-2 记忆去重自编辑（LLM 语义级判重，先行；失败降级回 Jaccard）
    # infer=False 时整段跳过：这一步会调 LLM 判定「新旧是否同一件事」
    # 并合成 merged_content，是确定性通路上最大的一处不确定来源。
    if infer:
        try:
            from ducky.self_edit import self_edit_on_add
            self_edit_result = self_edit_on_add(memory, user_id, messages_json, metadata, bank_id=bank_id)
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
                    user_id=user_id, bank_id=bank_id,
                )
                elapsed_ms = int((time.time() - start) * 1000)
                details["ms"] = elapsed_ms
                return {
                    "status": "ok",
                    "action": action,
                    "details": details,
                }
        except Exception as se:
            feature_failed("self_edit", se)
            logger.debug(f"self-edit 跳过（降级）: {se}")
    else:
        details["self_edit_skipped"] = "infer=false"

    # Step 1: 去重检查
    existing_id = dedup_check(memory, user_id, text, bank_id=bank_id)
    if existing_id:
        try:
            # Lethe v9.2.0: 触发演化追踪 (在更新前运行，便于捕获相似关系)
            track_knowledge_evolution(memory, user_id, text, existing_id, bank_id=bank_id, metadata=metadata)
            memory.update(existing_id, text, metadata=metadata)
            action = "updated"
            details["existing_id"] = existing_id
            logger.info(f"Layer1 去重更新: {existing_id[:16]}")
            # 🔴2：更新既有记忆后同步热度与 FTS，避免检索侧漏检/降权
            _sync_indexes_after_update(memory, memory_id=existing_id, content=text, user_id=user_id, bank_id=bank_id)
        except Exception as ue:
            # update 失败就走新增 —— 但**必须留痕**。
            # 去重命中却更新失败，结果是库里多出一条重复记忆，而返回的
            # action="new" 与「本来就是一条新记忆」在调用方看来一模一样。
            # 不记这一笔，坏掉的更新通路可以坏很久而没有任何东西发红：
            # 用户只会觉得「记忆怎么越来越重复」，查不到根因。
            # 语义不变（照旧降级新增），只是把降级这件事说出来。
            logger.warning(
                f"Layer1 去重更新失败，降级为新增: {existing_id[:16]} {type(ue).__name__}: {ue}"
            )
            details["dedup_update_failed"] = {
                "existing_id": existing_id,
                "error": f"{type(ue).__name__}: {str(ue)[:200]}",
            }
            add_result = memory.add(messages_json, user_id=user_id, metadata=metadata, infer=infer)
            _index_after_add(add_result, user_id=user_id, category=(metadata or {}).get("category"), bank_id=bank_id, infer=infer, metadata=metadata)
            action = "new"
    else:
        # Step 2: 容量检查
        cap = check_capacity(memory, user_id, bank_id=bank_id)
        details["capacity"] = cap
        if cap["needs_merge"]:
            merge_result = auto_merge_similar(memory, user_id, bank_id=bank_id)
            details["merge"] = merge_result
            action = "merged" if merge_result["merged_groups"] > 0 else "new"

        # Lethe v9.2.0: 写入前进行演化追踪，将可能被新记忆取代的旧记忆置为 superseded
        import hashlib
        try:
            new_id_placeholder = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
            track_knowledge_evolution(memory, user_id, text, new_id_placeholder, bank_id=bank_id, metadata=metadata)
        except Exception as e:
            logger.warning(f"写入前演化追踪失败: {e}")

        # Step 3: 写入
        # 🔴2：主链写入路径必须登记 salience + FTS 索引，否则新记忆全文搜不到、热度不累计。
        add_result = memory.add(messages_json, user_id=user_id, metadata=metadata, infer=infer)
        _index_after_add(add_result, user_id=user_id, category=(metadata or {}).get("category"), bank_id=bank_id, infer=infer, metadata=metadata)

    elapsed_ms = int((time.time() - start) * 1000)
    details["ms"] = elapsed_ms

    return {
        "status": "ok",
        "action": action,
        "details": details,
    }


def _index_after_add(add_result, user_id: str, category: str | None = None, bank_id: str = "default",
                     infer: bool = True, metadata: dict | None = None) -> None:
    """🔴2：mem0.add() 成功后登记 salience + 写 FTS 索引。

    正常新增路径此前只调 memory.add()，既不注册显著性、也不写全文索引，
    导致新记忆热度不累计、FTS/BM25 全文搜不到（向量召回不受影响）。
    此处统一补齐，任何一步失败静默降级不阻断写入。
    🔴7：同时按 AIDUMEM_TYPE_CLASSIFY_ENABLED 做写时六型分类落账本。
    """
    if add_result is None:
        return
    # v21.0 收口（生产用户审计 🔴-1）：出身打标落在本登记点——layer1 包装器吞掉
    # mem0 的 results，路由层拿不到 ref。infer=True（LLM 蒸馏经手）→ reasoned；
    # infer=False（确定性直写）→ user_provided。失败静默降级不阻断写入。
    try:
        from ducky.epistemic import stamp_memory_refs
        from ducky.origin_context import origin_from_metadata
        _refs = [
            r.get("id") or r.get("memory_id")
            for r in (add_result if isinstance(add_result, list)
                      else (add_result.get("results") if isinstance(add_result, dict) else []))
            if isinstance(r, dict)
        ]
        # v21.2.0 审计整改轮（生产用户审计 🔴-1）：origin 从**透传的 metadata** 显式取，
        # contextvar 只兜底。此前这里读 contextvar，等于给整条主链路埋了个
        # 「只要哪条通路没先 set 就静默变空」的隐式前提 —— 而空值不报错。
        _origin = origin_from_metadata(metadata)
        stamp_memory_refs(
            [r for r in _refs if r],
            "reasoned" if infer else "user_provided",
            user_id=user_id, bank_id=bank_id, source="add:layer1",
            origin=_origin,
        )
        # v21.2 M1：同一批 refs 登记为本 session 当前 episode 的一步。
        # 轨迹登记跟着打标走同一个缝（layer1 包装器吞掉 mem0 的 results，
        # 路由层拿不到 ref），并与打标共用同一份显式 origin。
        # 无 session（cron / 后台作业 / 调用方未传）一律不记，不稀释统计。
        try:
            from ducky.evolve_mem import record_episode_step
            if _origin[1]:
                record_episode_step([r for r in _refs if r], user_id=user_id,
                                    bank_id=bank_id, session_id=_origin[1])
        except Exception as _ee:
            logger.debug(f"episode step 登记跳过: {_ee}")
    except Exception as e:
        logger.debug(f"epistemic sidecar 打标跳过: {e}")
    try:
        from ducky.mem0_runtime import register_salience_for_add
        register_salience_for_add(add_result, user_id=user_id, bank_id=bank_id)
    except Exception as e:
        feature_failed("salience_register", e)
        logger.debug(f"salience 登记跳过: {e}")

    results = (
        add_result if isinstance(add_result, list)
        else (add_result.get("results") if isinstance(add_result, dict) else [])
    )
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        mid = r.get("id") or r.get("memory_id")
        content = r.get("memory") or r.get("data") or ""
        if not (mid and content):
            continue
        try:
            from ducky.text_fts import _index_memory
            _index_memory(mid, content, user_id=user_id, category=category, bank_id=bank_id)
        except Exception as e:
            feature_failed("index_memory", e)
            logger.debug(f"FTS index on add 跳过: {e}")
        _classify_memory_type_on_add(mid, content, user_id=user_id, bank_id=bank_id)


def _classify_memory_type_on_add(memory_id: str, content: str, *, user_id: str = "default", bank_id: str = "default") -> None:
    """🔴7：写时六型分类。默认关闭（规则分类），开 AIDUMEM_TYPE_CLASSIFY_ENABLED 后用 LLM。

    此前 classify_and_record 生产零调用、六型只能手动 backfill。这里接进主链，
    环境变量控制是否用 LLM；失败静默降级不阻断写入。
    """
    try:
        enabled = os.getenv("AIDUMEM_TYPE_CLASSIFY_ENABLED", "false").lower() in {"1", "true", "yes"}
        from ducky.memory_types import classify_and_record
        classify_and_record(memory_id, content, use_llm=enabled, user_id=user_id, bank_id=bank_id)
    except Exception as e:
        feature_failed("memory_type_classify", e)
        logger.debug(f"写时六型分类跳过: {e}")


def _sync_indexes_after_update(memory, memory_id: str, content: str, user_id: str, bank_id: str = "default") -> None:
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
        on_memory_added(memory_id, content=content, preserve_heat=True,
                        user_id=user_id, bank_id=bank_id)
    except Exception as e:
        feature_failed("evolve_on_added", e)
        logger.debug(f"self-edit 热度登记跳过: {e}")
    try:
        from ducky.text_fts import _index_memory
        # 🔴v20 甲14 故意不传 category：上面刚用 preserve_heat=True 保住了热度，
        # 这里原来却硬写 category=""，同一个函数里一半在保、一半在毁。合并只改
        # 内容不改分类，不传 = 让 _index_memory 沿用行上既有分类。
        _index_memory(memory_id, content, user_id=user_id, bank_id=bank_id)
    except Exception as e:
        feature_failed("index_memory", e)
        logger.debug(f"self-edit FTS 索引刷新跳过: {e}")


def track_knowledge_evolution(memory, user_id: str, new_text: str, new_id: str = "new_item",
                              bank_id: str = DEFAULT_BANK_ID, metadata: dict | None = None):
    """Lethe v9.2.0: 知识演化追踪 + 状态机流转

    v20 甲11 修复（跨库「标死」）
    ─────────────────────────────
    原来这里的检索是 ``filters={"user_id": user_id}``，不带 bank。同一函数体里
    另外五个兄弟调用（``dedup_check`` / ``_sync_indexes_after_update`` /
    ``_index_after_add`` / ``check_capacity`` / ``auto_merge_similar``）全都透传
    了 ``bank_id``，只有这一处和它的两个调用点漏了——是**漏项**，不是设计。

    后果不是「查不到」，是**改错别人家的账**：往 A 库写一条文本，检索会捞到
    B 库一条共用中文词的记忆，判成 ``replaces``，把 B 库那条写成
    ``memory_states.state='superseded'``；``recall_funnel`` 随后会把
    superseded 的条目从召回结果里剔掉（recall_funnel.py 的
    ``state = 'superseded'`` 那条 SQL）。于是 A 库的一次写入，让 B 库一条好端端
    的记忆**从此召回不到**。生产目前只有一个库，缺陷在位但还没打响。

    **不给 ``memory_states`` / ``knowledge_evolution`` 加作用域列。** 这两张表
    本来就零作用域列，是**全局平表**；只要「生成行的那次检索」按域收敛，表里
    就不可能出现跨库配对。这条不变量由负向对照守着，别为了「看起来更严谨」
    去加列——加了列反而要处理两套口径。

    过滤按 ``bank_contract`` 的两半契约走：``vector_scope_filters`` 负责下推，
    ``vector_item_in_bank`` 负责复筛。默认域**故意不下推** ``bank_id``（否则
    Qdrant 的 must 语义会把没有 bank_id 字段的 v19 存量点全判为不匹配，召回
    直接归零），所以默认域下**复筛是唯一承重的那一半**——而生产跑的正是默认域。
    """
    try:
        # 1. 查找最相似的候选记忆 (避开新写入的这一条)
        results = memory.search(new_text, filters=vector_scope_filters(user_id, bank_id), limit=5)
        results_list = results.get("results", results) if isinstance(results, dict) else results
        if not results_list:
            return

        for top in results_list:
            old_text = top.get("memory", "")
            old_id = top.get("id", "")
            if not old_text or not old_id or old_id == new_id:
                continue
            # 甲11 复筛：默认域没下推 bank_id，这一句是本域唯一的隔离屏障。
            # 缺字段的存量点按 default 算（vector_item_bank 的老语义），
            # 所以默认域仍然能正常演化 v19 老数据。
            if not vector_item_in_bank(top, bank_id):
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

            # 4. 保存演化关系到 facts.db（v21 F2：随带溯源三件套。
            #    v21.2.0 审计整改轮（🔴-1 同型加固）：从透传 metadata 显式取，
            #    contextvar 只兜底——隐式通道少一次 set 就静默变空。
            #    未迁移库（无列）如实退回旧五列写法。）
            from ducky.origin_context import origin_from_metadata
            _oa, _os, _ot = origin_from_metadata(metadata)
            conn = get_facts_conn()
            _ke_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(knowledge_evolution)").fetchall()}
            if "origin_agent" in _ke_cols:
                conn.execute(
                    "INSERT INTO knowledge_evolution (source_id, target_id, relation_type, confidence, reason,"
                    " origin_agent, origin_session_id, origin_turn) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (old_id, new_id, relation, sim, reason, _oa, _os, _ot)
                )
            else:
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

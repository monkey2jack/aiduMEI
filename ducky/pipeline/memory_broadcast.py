#!/usr/bin/env python3
"""
aiduMEM Memory Broadcast — 记忆广播链
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
J-space 启发：概念写入全局广播区后，被所有子电路读取→形成推理链。

— 核心逻辑 ─
1. 输入一条记忆 → 以它为 seed query 再检索引擎
2. 结果中的记忆继续作为 seed → 形成传播链
3. 链深度限制 (MAX_CHAIN_DEPTH=3)，去重控制

— 端点: POST /recall_chain
— 返回: 一组有序的记忆链，展示"记忆如何触发关联记忆"
"""

import time, logging
from typing import Optional

from ducky.bank_contract import DEFAULT_BANK_ID, vector_item_in_bank, vector_scope_filters
from ducky.utils import quick_sim

logger = logging.getLogger("aiduMEM.broadcast")

# ── 配置 ──
MAX_CHAIN_DEPTH = 3       # 链最大深度
CHAIN_BRANCH = 3          # 每层保留的候选数
CHAIN_MIN_SCORE = 0.3     # 传播最低分数阈值
CHAIN_TOTAL_MAX = 20      # 链总记忆数上限


def broadcast_chain(
    memory,
    seed_text: str,
    user_id: str,
    max_depth: int = MAX_CHAIN_DEPTH,
    branch: int = CHAIN_BRANCH,
    min_score: float = CHAIN_MIN_SCORE,
    total_max: int = CHAIN_TOTAL_MAX,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict:
    """
    从 seed_text 出发，逐层发现关联记忆，形成广播链。

    v20 P0-2：广播链的每一跳都按 (user_id, bank_id) 收窄——链式传播是
    最隐蔽的越库通道（seed 在本域，第二跳就可能摸进他库），所以复筛
    (vector_item_in_bank) 放在候选进链之前，跳数再多也翻不出域墙。

    返回 {
        chain: [                     # 按层级排列
            {
                depth: 0,
                seed: "...",         # 本层输入的 query
                results: [{id, memory, score, _broadcast_depth}, ...]
            },
            ...
        ],
        total: int,                  # 链总记忆数
        seed: str,                   # 原始 seed
    }
    """
    t0 = time.time()
    # 非法作用域在进链前炸出 BankScopeError（路由层包成 error dict）
    scope_filters = vector_scope_filters(user_id, bank_id)
    chain = []
    seen_ids = set()
    current_seeds = [seed_text]

    for depth in range(max_depth):
        level_results = []
        next_seeds = []

        for seed in current_seeds:
            if not seed or len(seed.strip()) < 3:
                continue

            try:
                raw = memory.search(seed, filters=scope_filters, limit=branch)
                candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
                if not isinstance(candidates, list):
                    candidates = []
                # 默认域不下推 bank_id（存量向量无此字段），靠复筛隔离
                candidates = [c for c in candidates if vector_item_in_bank(c, bank_id)]
            except Exception as e:
                logger.warning(f"广播链搜索失败 (depth={depth}, seed={seed[:30]}): {e}")
                continue

            for item in candidates:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id", "")
                if not mid or mid in seen_ids:
                    continue
                # 过滤低分
                score = item.get("score", 0) or 0
                if score < min_score:
                    continue
                # 过滤 self-match（完全一样的文本）
                mem_text = item.get("memory", "")
                if mem_text and _self_sim(mem_text, seed) > 0.9:
                    continue

                seen_ids.add(mid)
                item["_broadcast_depth"] = depth
                item["_broadcast_seed"] = seed[:40]
                level_results.append(item)

                # 候选下一层的 seed
                if depth < max_depth - 1 and len(next_seeds) < branch:
                    next_seeds.append(mem_text)

            # 总量控制
            if len(chain) + len(level_results) >= total_max:
                break

        if level_results:
            chain.append({
                "depth": depth,
                "seed": current_seeds[0][:60] if current_seeds else "",
                "results": level_results,
            })
        else:
            break  # 无更多记忆可传播

        current_seeds = next_seeds
        if not current_seeds:
            break

    # 清理内部标记
    total = 0
    for level in chain:
        for item in level["results"]:
            item.pop("_broadcast_depth", None)
            item.pop("_broadcast_seed", None)
        total += len(level["results"])

    elapsed = int((time.time() - t0) * 1000)
    logger.info(f"📡 Broadcast chain: {total} 条, {len(chain)} 层, {elapsed}ms")

    return {
        "chain": chain,
        "total": total,
        "seed": seed_text,
        "ms": elapsed,
    }


def broadcast_expand(memory, memory_id: str, user_id: str, limit: int = 5,
                     bank_id: str = DEFAULT_BANK_ID) -> list:
    """
    单次广播：从一条记忆出发，找回关联记忆。

    简化版——不走链，只做一次邻居搜索。
    v20 P0-2：源记忆定位与邻居搜索都按 (user_id, bank_id) 收窄——
    他库的 memory_id 在本域视角下就是「找不到」。
    """
    try:
        scope_filters = vector_scope_filters(user_id, bank_id)
        # 先拿文本（复筛保证他库同 id 的记忆定位不到）
        all_mem = memory.get_all(filters=scope_filters, limit=10000)
        results_list = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        source_text = ""
        for item in (results_list or []):
            if (isinstance(item, dict) and item.get("id") == memory_id
                    and vector_item_in_bank(item, bank_id)):
                source_text = item.get("memory", "")
                break

        if not source_text:
            logger.warning(f"广播展开：找不到记忆 {memory_id[:16]}")
            return []

        raw = memory.search(source_text, filters=scope_filters, limit=limit + 1)
        candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
        if not isinstance(candidates, list):
            return []
        candidates = [c for c in candidates if vector_item_in_bank(c, bank_id)]

        # 排除自身
        return [item for item in candidates if item.get("id") != memory_id][:limit]
    except Exception as e:
        logger.warning(f"广播展开失败: {e}")
        return []


def _self_sim(a: str, b: str) -> float:
    """检查两段文本是否几乎一样"""
    if not a or not b:
        return 0.0
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))

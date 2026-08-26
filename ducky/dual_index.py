"""ducky.dual_index — 双索引（v20.2 WP-F · 智慧引擎自动挡）

云向量（1024 维，collection `mem0`）与本地向量（512 维，collection
`mem0_local`）是两种语言，**必须分库、绝不混搜**。本模块管本地这一库：

  - 复用运行中 mem0 已打开的 qdrant client 操作 `mem0_local` ——
    **绝不自开第二个进程/第二个 client 连本地 qdrant**（R-17 铁律：
    嵌入式 qdrant 单进程，双开即锁冲突）。
  - 点位 id 与云侧**同源**：同一条记忆在两库用同一个 id ——
    删除链一把钥匙开两把锁，对账才有「腿」可对。
  - payload 契约与云侧对齐（data/user_id/bank_id/…），装配与复筛
    读侧零改动。
  - 每一次写失败都进欠账账本（pending_local_embeddings 概念并入
    cloud 欠账同一张表 pending_embeddings，side 字段区分）——
    软失败不抛不丢，恢复后补算（backfill/对账同款方法论）。

lite 挡写入语义（与预案 WP-F 对齐）：云嵌入不可用时 mem0 主体（LLM
蒸馏 + 云向量）整体欠账（原始 add 载荷进 pending_cloud_adds），
verbatim/facts 照落（不依赖嵌入），verbatim 原文补本地向量 ——
lite 挡语义召回的语料 = 本地库里的「蒸馏记忆本地副本 + lite 期原文向量」。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ducky.local_embed import LOCAL_EMBED_DIM, local_embed_texts

logger = logging.getLogger("aiduMEM.dual_index")

LOCAL_COLLECTION = "mem0_local"


def _qdrant_client():
    """借运行中 mem0 的 qdrant client（同进程复用，R-17 合规）。"""
    from ducky.mem0_runtime import get_memory
    return get_memory().vector_store.client


def ensure_local_collection(client=None) -> None:
    from qdrant_client import models as qm
    client = client or _qdrant_client()
    existing = {c.name for c in client.get_collections().collections}
    if LOCAL_COLLECTION not in existing:
        client.create_collection(
            collection_name=LOCAL_COLLECTION,
            vectors_config=qm.VectorParams(size=LOCAL_EMBED_DIM,
                                           distance=qm.Distance.COSINE),
        )
        logger.info("🪫 本地向量库 %s 已建（%d 维）", LOCAL_COLLECTION, LOCAL_EMBED_DIM)


def upsert_local(point_id: str, text: str, payload: Dict[str, Any],
                 client=None) -> bool:
    """写一条本地向量；失败进欠账不抛（软失败三副本纪律）。"""
    try:
        from qdrant_client import models as qm
        client = client or _qdrant_client()
        ensure_local_collection(client)
        vec = local_embed_texts([text])[0]
        client.upsert(
            collection_name=LOCAL_COLLECTION,
            points=[qm.PointStruct(id=point_id, vector=vec, payload=payload)],
        )
        return True
    except Exception as exc:
        try:
            from ducky.failure_ledger import feature_failed
            feature_failed("dual_index_local", exc)
        except Exception:
            pass
        _enqueue_pending("local", point_id, text, payload)
        logger.debug("本地向量写入欠账 %s: %s", point_id, exc)
        return False


def upsert_local_verbatim(user_id: str, bank_id: str, text: str,
                          client=None) -> bool:
    """原文本地向量（lite 挡召回语料）：id 由 (原文哈希, 域) 确定性派生 ——
    同一句原文重发不堆点（与 verbatim 幂等去重同语义）。"""
    import hashlib
    import uuid
    from datetime import datetime
    text = (text or "").strip()
    if not text:
        return False
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    pid = str(uuid.uuid5(uuid.NAMESPACE_URL,
                         f"aidumei:verbatim_local::{digest}::{user_id}::{bank_id}"))
    now = datetime.now().isoformat()
    payload = {
        "data": text[:2000],
        "hash": digest,
        "created_at": now,
        "user_id": user_id,
        "bank_id": bank_id,
        "memory_class": "verbatim_local",
        "recorded_at": now,
    }
    return upsert_local(pid, text[:2000], payload, client=client)


def delete_local(point_ids: List[str], client=None) -> int:
    """删除链的本地腿：按 id 精确删（与云侧同一把钥匙）。"""
    if not point_ids:
        return 0
    try:
        client = client or _qdrant_client()
        existing = {c.name for c in client.get_collections().collections}
        if LOCAL_COLLECTION not in existing:
            return 0
        client.delete(collection_name=LOCAL_COLLECTION, points_selector=point_ids)
        return len(point_ids)
    except Exception as exc:
        logger.warning("本地向量删除失败（%d 点）: %s", len(point_ids), exc)
        return 0


def delete_local_by_scope(user_id: str, bank_id: str = "default", client=None) -> int:
    """delete_all 的本地腿：payload 谓词删（与云侧枚举复筛同语义）。"""
    try:
        from qdrant_client import models as qm
        client = client or _qdrant_client()
        existing = {c.name for c in client.get_collections().collections}
        if LOCAL_COLLECTION not in existing:
            return 0
        flt = qm.Filter(must=[
            qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
            qm.FieldCondition(key="bank_id", match=qm.MatchValue(value=bank_id)),
        ])
        before = client.count(LOCAL_COLLECTION, count_filter=flt, exact=True).count
        if before:
            client.delete(collection_name=LOCAL_COLLECTION,
                          points_selector=qm.FilterSelector(filter=flt))
        return int(before)
    except Exception as exc:
        logger.warning("本地向量按域删除失败 user=%s: %s", user_id, exc)
        return 0


def search_local(query: str, user_id: str, bank_id: str = "default",
                 limit: int = 20, client=None) -> List[dict]:
    """lite 挡召回腿：查询用**本地模型**嵌入（与索引同语言），结果装配
    对齐 mem0 结果契约（memory/score/metadata），上层管线零改动。"""
    from qdrant_client import models as qm
    client = client or _qdrant_client()
    existing = {c.name for c in client.get_collections().collections}
    if LOCAL_COLLECTION not in existing:
        return []
    qv = local_embed_texts([query])[0]
    flt = qm.Filter(must=[
        qm.FieldCondition(key="user_id", match=qm.MatchValue(value=user_id)),
        qm.FieldCondition(key="bank_id", match=qm.MatchValue(value=bank_id)),
    ])
    # 生产实测（2026-08-27 质量对照窗口）：新版 qdrant-client 已移除
    # `search`，只有 `query_points` —— 而测试替身实现了 search，制造了
    # 「lite 向量腿在生产其实一直 AttributeError、命中全靠 BM25 兜底」的
    # 假绿灯。query_points 优先，老版本回落 search；替身也被改造成
    # 只有 query_points（对齐生产 API 面，见 test_v20_2_autoshift）。
    if hasattr(client, "query_points"):
        hits = client.query_points(collection_name=LOCAL_COLLECTION, query=qv,
                                   query_filter=flt, limit=limit,
                                   with_payload=True).points
    else:
        hits = client.search(collection_name=LOCAL_COLLECTION, query_vector=qv,
                             query_filter=flt, limit=limit, with_payload=True)
    out = []
    for h in hits:
        pl = dict(h.payload or {})
        out.append({
            "id": str(h.id),
            "memory": pl.get("data", ""),
            "score": float(h.score),
            "created_at": pl.get("created_at"),
            "user_id": pl.get("user_id"),
            "metadata": {k: v for k, v in pl.items() if k not in ("data", "user_id")},
        })
    return out


def local_point_count(client=None) -> Optional[int]:
    try:
        client = client or _qdrant_client()
        existing = {c.name for c in client.get_collections().collections}
        if LOCAL_COLLECTION not in existing:
            return 0
        return client.count(LOCAL_COLLECTION, exact=True).count
    except Exception:
        return None


# ── 欠账账本（pending_embeddings）────────────────────────────────────
# side='cloud'：lite 挡期间整笔 add 的云侧欠账（payload = 原始请求）。
# side='local'：本地向量单点写失败的欠账（payload = 点位重建材料）。
# 两侧都在恢复/启动对账时重放；表进删除链矩阵（含用户内容，按域清）。

def ensure_pending_schema() -> None:
    from ducky.utils import get_facts_conn
    conn = get_facts_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_embeddings (
                pending_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                side        TEXT NOT NULL,
                ref_id      TEXT NOT NULL DEFAULT '',
                payload     TEXT NOT NULL DEFAULT '{}',
                user_id     TEXT NOT NULL DEFAULT '',
                bank_id     TEXT NOT NULL DEFAULT 'default',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                replayed_at TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _enqueue_pending(side: str, ref_id: str, text: str,
                     payload: Dict[str, Any]) -> None:
    try:
        from ducky.utils import get_facts_conn
        ensure_pending_schema()
        conn = get_facts_conn()
        try:
            conn.execute(
                "INSERT INTO pending_embeddings (side, ref_id, payload, user_id, bank_id) "
                "VALUES (?,?,?,?,?)",
                (side, ref_id,
                 json.dumps({"text": text, "payload": payload}, ensure_ascii=False),
                 str(payload.get("user_id", "")), str(payload.get("bank_id", "default"))),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("欠账入账失败（%s/%s）: %s", side, ref_id, exc)


def enqueue_cloud_add(request_payload: Dict[str, Any], user_id: str,
                      bank_id: str = "default") -> None:
    """lite 挡写入：整笔 add 的云侧欠账（恢复后重放走完整 mem0 管线）。"""
    try:
        from ducky.utils import get_facts_conn
        ensure_pending_schema()
        conn = get_facts_conn()
        try:
            conn.execute(
                "INSERT INTO pending_embeddings (side, ref_id, payload, user_id, bank_id) "
                "VALUES ('cloud','add',?,?,?)",
                (json.dumps(request_payload, ensure_ascii=False), user_id, bank_id),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("🪫 lite 挡写入：云侧蒸馏欠账 +1（user=%s）", user_id)
    except Exception as exc:
        logger.warning("云侧欠账入账失败: %s", exc)


def pending_counts() -> Dict[str, int]:
    """/health 欠账水位。"""
    try:
        from ducky.utils import get_facts_conn
        ensure_pending_schema()
        conn = get_facts_conn()
        try:
            rows = conn.execute(
                "SELECT side, COUNT(*) FROM pending_embeddings "
                "WHERE replayed_at IS NULL OR replayed_at='claiming' "
                "GROUP BY side").fetchall()
            out = {"cloud": 0, "local": 0}
            for side, n in rows:
                out[str(side)] = int(n)
            return out
        finally:
            conn.close()
    except Exception:
        return {"cloud": -1, "local": -1}


def replay_pending(*, apply: bool = True, limit: int = 200) -> Dict[str, Any]:
    """欠账重放（升挡后/启动对账调用）。cloud 侧重放整笔 add（完整蒸馏），
    local 侧重放单点 upsert。逐条独立：一条失败不拖垮批次，留在账上下轮再来。"""
    from ducky.utils import get_facts_conn
    ensure_pending_schema()
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            "SELECT pending_id, side, ref_id, payload, user_id, bank_id "
            "FROM pending_embeddings WHERE replayed_at IS NULL "
            "ORDER BY pending_id LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    report = {"scanned": len(rows), "replayed": 0, "failed": 0,
              "skipped_claimed_or_deleted": 0, "apply": apply}
    if not apply:
        return report
    for pid, side, ref_id, payload_json, user_id, bank_id in rows:
        # 竞态闭合（自审发现，w 式组合拳变体）：本循环拿的是快照行 ——
        # 若 delete_all 在重放执行前清了该租户，快照行照跑就是「已删内容
        # 以补蒸馏名义复活」。原子抢占：UPDATE ... WHERE replayed_at IS NULL
        # 只会命中仍然在账的行；被 delete_all 删掉或被并发重放拿走的行
        # rowcount=0，跳过。残余窗口（抢占后、mem.add 完成前的同租户
        # delete_all 交叉，秒级）如实登记于矩阵理由，不冒充零。
        cconn = get_facts_conn()
        try:
            cur = cconn.execute(
                "UPDATE pending_embeddings SET replayed_at='claiming' "
                "WHERE pending_id=? AND replayed_at IS NULL", (pid,))
            cconn.commit()
            claimed = int(cur.rowcount or 0) == 1
        finally:
            cconn.close()
        if not claimed:
            report["skipped_claimed_or_deleted"] += 1
            continue
        try:
            data = json.loads(payload_json)
            if side == "cloud":
                from ducky.mem0_runtime import get_memory
                from ducky.bank_contract import stamp_bank_metadata
                mem = get_memory()
                mem.add(data.get("messages", ""), user_id=user_id,
                        metadata=stamp_bank_metadata(data.get("metadata") or {}, bank_id))
            else:
                ok = upsert_local(ref_id, data.get("text", ""),
                                  data.get("payload") or {})
                if not ok:
                    raise RuntimeError("本地补写仍失败")
            conn = get_facts_conn()
            try:
                conn.execute(
                    "UPDATE pending_embeddings SET replayed_at=CURRENT_TIMESTAMP "
                    "WHERE pending_id=?", (pid,))
                conn.commit()
            finally:
                conn.close()
            report["replayed"] += 1
        except Exception as exc:
            report["failed"] += 1
            logger.warning("欠账重放失败 #%s（留账下轮）: %s", pid, exc)
            try:  # 失败回滚抢占标记，下轮再来
                rconn = get_facts_conn()
                try:
                    rconn.execute(
                        "UPDATE pending_embeddings SET replayed_at=NULL "
                        "WHERE pending_id=? AND replayed_at='claiming'", (pid,))
                    rconn.commit()
                finally:
                    rconn.close()
            except Exception:
                pass
    return report


def delete_pending_by_scope(user_id: str, bank_id: str = "default") -> int:
    """delete_all 的欠账腿：欠账载荷含用户原文，删除链必须清。"""
    try:
        from ducky.utils import get_facts_conn
        ensure_pending_schema()
        conn = get_facts_conn()
        try:
            cur = conn.execute(
                "DELETE FROM pending_embeddings WHERE user_id=? AND bank_id=?",
                (user_id, bank_id))
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("欠账按域清理失败: %s", exc)
        return 0

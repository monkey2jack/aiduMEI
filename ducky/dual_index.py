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


def _local_off() -> bool:
    """云端档：本地腿整条关闭（不写、不搜、不加载模型）。写入侧静默跳过
    是对的 —— 部署方**明确选择了**没有备胎，那不是故障，不该进欠账。"""
    try:
        from ducky.engine_mode import local_leg_enabled
        return not local_leg_enabled()
    except Exception:
        return False


def upsert_local(point_id: str, text: str, payload: Dict[str, Any],
                 client=None, *, enqueue_on_fail: bool = True) -> bool:
    """写一条本地向量；失败进欠账不抛（软失败三副本纪律）。云端档整条跳过。

    enqueue_on_fail（v20.2.1 外审 R4）：重放路径必须传 False —— 重放
    失败时这里再入一笔新账、外层又把原行回滚待重放，每轮净增 1 条，
    模型持续故障下欠账表指数自我复制。失败语义由调用方决定：常规写
    失败=入账（补算契约），重放失败=只回滚原行（留账下轮）。"""
    if _local_off():
        return False
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
        if enqueue_on_fail:
            _enqueue_pending("local", point_id, text, payload)
        logger.debug("本地向量写入欠账 %s: %s", point_id, exc)
        return False


def verbatim_local_pid(user_id: str, bank_id: str, text: str) -> str:
    """verbatim 本地点的确定性 id（纯函数，v20.2.1 外审 R3 抽出）。

    这类点的 id 由 (原文, 域) 派生而非 memory_id 同源 —— 一条原文可
    蒸出多条记忆，天然一对多，没有唯一的 memory_id 可同源。单条删除
    链要够到它，必须拿被删记忆的正文**重演同一套派生**（wal_engine §8b
    搭车 §0a 抓到的正文调本函数）。改这里的派生公式 = 同时改写入与
    删除两侧，绝不许只改一边。"""
    import hashlib
    import uuid
    digest = hashlib.md5((text or "").strip().encode("utf-8")).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"aidumei:verbatim_local::{digest}::{user_id}::{bank_id}"))


def upsert_local_verbatim(user_id: str, bank_id: str, text: str,
                          client=None) -> bool:
    """原文本地向量（lite 挡召回语料）：id 由 (原文哈希, 域) 确定性派生 ——
    同一句原文重发不堆点（与 verbatim 幂等去重同语义）。"""
    import hashlib
    from datetime import datetime
    text = (text or "").strip()
    if not text or _local_off():
        return False
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    pid = verbatim_local_pid(user_id, bank_id, text)
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
    if _local_off():
        return []
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


# v20.2.3（自查 S-3）：欠账账本**无上界**——备胎模型若持续损坏，
# 每次写入 +1 行永久增长，而 /health 此前只报一个裸数字、**没有任何
# 判据说多少算不健康**（重演了 refine_memory「阈值是拍脑袋常数」的老
# 问题，这次是连常数都没有）。下面这个阈值同样是**工程惯例值、非实测**，
# 按老规矩显式标注待生产分布校准——但「有个会说话的判据」比「一个没人
# 看得懂的数字」强，这是水位探针存在的意义。
_PENDING_WARN_ENV = "AIDUMEI_PENDING_WARN_LEVEL"
_DEFAULT_PENDING_WARN = 500


def pending_warn_level() -> int:
    from ducky.env_config import int_env
    return int_env(_PENDING_WARN_ENV, _DEFAULT_PENDING_WARN, minimum=0)


def pending_verdict(counts: Dict[str, int]) -> Dict[str, Any]:
    """把裸水位翻译成判语：ok / elevated / stuck。

    stuck 的判据不是「数字大」，而是「数字大**且**最近一次重放没能清掉」
    ——单看数字会把「刚断供完、正在排队补算」误报成故障。
    """
    total = max(counts.get("cloud", 0), 0) + max(counts.get("local", 0), 0)
    level = pending_warn_level()
    if level <= 0 or total < level:
        return {"level": "ok", "total": total, "warn_at": level}
    last = last_replay_status()
    drained = bool(last and (last.get("report") or {}).get("replayed"))
    return {
        "level": "elevated" if drained else "stuck",
        "total": total,
        "warn_at": level,
        "hint": ("欠账水位偏高但上次重放确有进展，属补算排队"
                 if drained else
                 "欠账水位偏高且上次重放未清掉任何一条——请查本地嵌入模型"
                 "是否损坏（/health 的 local_embed.load_error）或云侧是否仍断供"),
    }


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


_last_replay: Dict[str, Any] = {}


def last_replay_status() -> Optional[dict]:
    """/health 用：最近一次欠账重放的时间、触发源与结果（进程内，
    重启归 None —— 与挡位同语义：重启是重新认识世界）。"""
    return dict(_last_replay) or None


def unreplayed_count() -> int:
    """待重放行数（不含 claiming —— 那是正被别的线程还着的账）。"""
    try:
        from ducky.utils import get_facts_conn
        ensure_pending_schema()
        conn = get_facts_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM pending_embeddings "
                "WHERE replayed_at IS NULL").fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()
    except Exception:
        return -1


def spawn_replay_daemon(source: str) -> bool:
    """守护线程重放欠账 —— 升挡事件与启动对账两个触发点共用。

    v20.2.1（外审 R2）：重放此前只挂在「升挡事件」上，而重启把挡位重置
    回 closed，升挡事件重启后永不再来 —— lite 期欠账成永久赖账。
    reconcile_startup 末尾兜底扫一遍即闭合。零欠账不起线程；claiming
    原子抢占已防两个触发点并发。返回是否真的起了线程。"""
    import threading
    if unreplayed_count() <= 0:
        return False

    def _run():
        try:
            report = replay_pending(apply=True, source=source)
            logger.info("⚙️ 欠账重放（%s）：%s", source, report)
        except Exception as exc:
            logger.warning("欠账重放失败（%s，留账下轮）: %s", source, exc)

    global _replay_thread
    _replay_thread = threading.Thread(target=_run, name=f"pending-replay-{source}",
                                      daemon=True)
    _replay_thread.start()
    return True


_replay_thread = None  # 最近一次重放线程句柄（join_replay_for_tests 用）


def join_replay_for_tests(timeout: float = 10.0) -> None:
    """测试收尾：等重放守护线程真正结束。

    线程活过测试边界 = 活进了别人的猴补丁世界——FACTS_DB 补丁被还原后，
    线程的下一次连接摸到的就是别的库，日志落进别的测试的 caplog 窗口
    （全轴序下的闪烁红灯就是这么来的）。生产不需要这把手（环境不换），
    测试必须收尾。"""
    t = _replay_thread
    if t is not None and t.is_alive():
        t.join(timeout)


def _revoke_replayed_add(mem, added) -> int:
    """残窗补偿（v20.2.1 · 外部审计建议采纳）：claiming 抢占后、mem.add
    完成前，同租户 delete_all 交叉 —— 账本行已被按域清掉，而 add 刚把
    内容写回云侧 = 已删内容复活。把刚重放的点当场撤销：**用户的删除
    意愿优先于补算完整性**。逐点独立撤销，单点失败不拖垮其余。"""
    ids = []
    if isinstance(added, dict):
        for r in added.get("results") or []:
            mid = (r or {}).get("id") if isinstance(r, dict) else None
            if mid:
                ids.append(str(mid))
    revoked = 0
    for mid in ids:
        try:
            mem.delete(mid)
            revoked += 1
        except Exception as exc:
            logger.warning("残窗补偿撤销失败 %s: %s", mid, exc)
    logger.warning("🪫 重放残窗补偿：同租户 delete_all 交叉，撤销刚重放的 %d/%d 点",
                   revoked, len(ids))
    return revoked


def replay_pending(*, apply: bool = True, limit: int = 200,
                   source: str = "manual") -> Dict[str, Any]:
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
              "skipped_claimed_or_deleted": 0, "revoked_after_scope_delete": 0,
              "apply": apply}
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
                added = mem.add(data.get("messages", ""), user_id=user_id,
                                metadata=stamp_bank_metadata(data.get("metadata") or {}, bank_id))
                # 残窗闭合（v20.2.1 · 外部审计建议）：add 完成后复核账本行
                # 还在不在 —— 按域清账（delete_all）连 claiming 行一起删，
                # 行没了即证明窗口内该租户要求了删除。撤销刚写入的点。
                vconn = get_facts_conn()
                try:
                    still = vconn.execute(
                        "SELECT 1 FROM pending_embeddings WHERE pending_id=?",
                        (pid,)).fetchone()
                finally:
                    vconn.close()
                if still is None:
                    _revoke_replayed_add(mem, added)
                    report["revoked_after_scope_delete"] += 1
                    continue
            else:
                # R4：重放失败不许再入新账（enqueue_on_fail=False），
                # 只走下方回滚原行 —— 否则每轮净增 1 条自我复制。
                ok = upsert_local(ref_id, data.get("text", ""),
                                  data.get("payload") or {},
                                  enqueue_on_fail=False)
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
    from datetime import datetime
    _last_replay.clear()
    _last_replay.update({"at": datetime.now().isoformat(timespec="seconds"),
                         "source": source, "report": dict(report)})
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

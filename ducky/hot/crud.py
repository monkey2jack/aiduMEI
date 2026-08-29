"""ducky.hot.crud — recent/stats/delete/update/usage/reload/inject"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from ducky.api_models import (
    DeleteAllRequest,
    DeleteRequest,
    GovernanceReviewRequest,
    InjectContextRequest,
    OpinionSetRequest,
    TombstoneRestoreRequest,
    UpdateRequest,
)
from ducky.utils import DEFAULT_USER_ID
from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    ensure_bank_registered,
    make_scope,
    vector_item_in_bank,
    vector_scope_filters,
)
from ducky.mem0_runtime import (
    _normalize_user_id,
    get_llm_usage,
    get_memory,
    reset_memory_singleton,
)
from ducky.wal_engine import cascade_delete_memory, cascade_delete_all
from ducky.failure_ledger import feature_failed
from ducky.api_errors import api_error_detail

logger = logging.getLogger("aiduMEM.hot")


def register_crud_routes(app: FastAPI) -> None:
    @app.get("/recent")
    def recent(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID, limit: int = 10):
        try:
            scope = make_scope(user_id, bank_id)
            mem = get_memory()
            # 🔴v20：过滤下推 + Python 复筛，缺一不可。直接把 bank_id 塞进
            # filters 会把**所有**存量向量滤掉（payload 里没这个字段）。
            raw = mem.get_all(
                filters=vector_scope_filters(_normalize_user_id(scope.user_id), scope.bank_id),
                limit=limit,
            )
            items = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
            kept = [it for it in items if vector_item_in_bank(it, scope.bank_id)]
            results = {**raw, "results": kept} if isinstance(raw, dict) else kept
            return {"status": "ok", "results": results}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"recent 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    @app.get("/stats")
    def stats(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID):
        try:
            scope = make_scope(user_id, bank_id)
            mem = get_memory()
            user_id = _normalize_user_id(scope.user_id)
            all_mem = mem.get_all(filters=vector_scope_filters(user_id, scope.bank_id), limit=10000)
            results = all_mem.get("results", []) if isinstance(all_mem, dict) else (all_mem or [])
            # 同上：默认域没下推 bank_id，命名域的点得在这儿剔掉，否则 /stats
            # 会把别的域的条数算进默认域。
            results = [it for it in results if vector_item_in_bank(it, scope.bank_id)]

            total = len(results)
            hash_counts: dict = {}
            user_counts: dict = {}
            tag_counts: dict = {}

            for item in results:
                h = item.get("hash", "")
                item_uid = item.get("user_id", user_id)
                mem_text = item.get("memory", "")
                user_counts[item_uid] = user_counts.get(item_uid, 0) + 1
                if h:
                    hash_counts[h] = hash_counts.get(h, 0) + 1
                if mem_text and mem_text.startswith("["):
                    tag = mem_text.split("]")[0] + "]"
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

            dupes = {h: c for h, c in hash_counts.items() if c > 1}
            total_dupes = sum(c - 1 for c in dupes.values())

            # --- 多模态记忆统计 (v18.3 · v19.4.1 补租户收窄) ---
            #
            # 🟡（v19.4.1 用户审计）：这两个计数原本是全库 COUNT(*)，不带租户条件。
            #     其余字段都随 user_id 变化，唯独它们恒定 —— 于是陌生租户查
            #     /stats 时会看到 total=0 但 vision_count=1136，从计数即可推断
            #     本机记忆总规模。属于侧信道信息泄漏（量级泄漏，非内容泄漏）。
            #     现按 tenant_clause 同一套可见性规则收窄，与 /facts 等路由一致。
            vision_count = 0
            obsidian_count = 0
            try:
                from ducky.facts_recall import tenant_clause
                from ducky.utils import get_facts_conn
                conn = get_facts_conn()
                # v20：传 conn 让 tenant_clause 感知 bank_id 列——已迁移库里
                # 计数也要按 (user_id, bank_id) 收窄，否则默认租户的 /stats
                # 会把具名域的多模态条数算进来（量级泄漏的 bank 版）。
                t_clause, t_params = tenant_clause(
                    user_id, bank_id=scope.bank_id, conn=conn
                )
                vision_count = conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE media_url IS NOT NULL" + t_clause,
                    t_params,
                ).fetchone()[0]
                obsidian_count = conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE source = 'obsidian'" + t_clause,
                    t_params,
                ).fetchone()[0]
                conn.close()
            except Exception as _e:
                logger.warning(f"统计多模态/obsidian数据异常: {_e}")

            return {
                "status": "ok",
                "total": total,
                "total_memories": total,
                "duplicate_count": total_dupes,
                "user_id": user_id,
                "bank_id": scope.bank_id,
                "user_distribution": user_counts,
                "unique_hashes": len(hash_counts),
                "duplicate_hashes": len(dupes),
                "after_dedup": total - total_dupes,
                "top_tags": dict(sorted(tag_counts.items(), key=lambda x: -x[1])[:10]),
                "vision_count": vision_count,
                "obsidian_count": obsidian_count,
                "memories": all_mem,
            }
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"stats 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    @app.post("/delete")
    def delete(req: DeleteRequest):
        # 🔴P0-1: 传递并严格校验 user_id 归属，杜绝跨租户越权删除
        if not req.memory_id or not req.memory_id.strip():
            raise HTTPException(400, "memory_id 不能为空")
        try:
            scope = make_scope(req.user_id, req.bank_id)
            user_id = _normalize_user_id(scope.user_id) if scope.user_id else DEFAULT_USER_ID
            res = cascade_delete_memory(req.memory_id, user_id=user_id, bank_id=scope.bank_id)
            # v20.2.5-b（生产实机冒烟 D2）：**透传底层三态**。
            #
            # 这一行原先硬编码 `{"status": "ok"}` —— 与 `delete_all` 出口曾经
            # 犯的是同一个错，而本版只修了那一个。于是 README 写着「删除结果
            # 三态」，对**单条删除**不成立：调用方拿不到 failed_layers，
            # 也拿不到 not_cleared，任何一层失败都被抹平成 ok。
            #
            # `not_found` 走 200：DELETE 按 REST 惯例幂等，删一个已经不在的
            # 东西不是错误（consolidator 正在批量做这件事）。变的是**状态字段
            # 不再说谎** —— 「一层都没命中」是可读出来的事实。
            outcome = res.get("status", "failed")
            body = {
                "status": outcome,
                "details": res.get("details", {}),
                "failed_layers": res.get("failed_layers", []),
                "not_cleared": res.get("not_cleared", []),
            }
            if outcome in ("committed", "not_found"):
                return body
            if outcome == "partial":
                return JSONResponse(status_code=207, content=body)
            return JSONResponse(status_code=500, content=body)
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"delete 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    @app.post("/delete_all")
    def delete_all(req: DeleteAllRequest):
        # 🔴P0-3: 强制显式指定 user_id，清空 default 全库必须二次确认 confirm=True
        if not req.user_id or not req.user_id.strip():
            raise HTTPException(400, "user_id 必须显式指定，拒绝空参数清库")
        scope = make_scope(req.user_id, req.bank_id)
        user_id = _normalize_user_id(scope.user_id)
        # v20.1.1（N-1）：删除路径限流（默认 3/min）——生产 14 天 delete_all
        # 共 7 次，正常操作打不到上限；循环误删在清空更多域之前被拦停。
        from ducky.rate_guard import check_rate, delete_all_rate_limit
        _retry = check_rate("delete_all", user_id, limit=delete_all_rate_limit())
        if _retry is not None:
            raise HTTPException(
                status_code=429,
                detail=f"delete_all 频率超限（租户 {user_id}）：{_retry}s 后重试；"
                       f"上限可经 AIDUMEI_RATE_DELETE_ALL_PER_MIN 调整（0=关闭）",
                headers={"Retry-After": str(_retry)},
            )
        if user_id == DEFAULT_USER_ID and not getattr(req, "confirm", False):
            # v19.4.2：文案原先把默认身份写死成 "(default)"。部署方配了
            # AIDUMEM_DEFAULT_USER_ID 之后，报错里说的租户和实际要清的
            # 租户不是同一个，运维照着文案排查会走岔。改成回报真实身份。
            raise HTTPException(400, f"清空默认用户({user_id})全部记忆具有破坏性，必须传递 confirm: true 二次确认")

        try:
            res = cascade_delete_all(user_id=user_id, bank_id=scope.bank_id, confirm=getattr(req, "confirm", False))
            # v20.2.5（外审 F-02）：**透传底层的三态判决**。
            #
            # 这一行原先硬编码 `{"status": "ok"}` —— 底层无论返回什么都被抹平成
            # ok，连 v20.2.4 加的 `not_cleared` 也**从没到达过调用方**（那条
            # 「如实告知没清什么」的修复因此是半假的：底层加了，出口没透）。
            # 与 F-03 同型：改了代码，但在链路的另一端断掉。
            #
            # HTTP 状态跟着业务状态走 —— 外审门槛要的是「注入任意一层故障，
            # HTTP 与业务状态都必须显式失败」。207 会强制调用方注意到
            # 「不是完全成功」，而 200 + 一个藏在 body 里的字段不会。
            outcome = res.get("status", "failed")
            body = {
                "status": outcome,
                "details": res.get("details", {}),
                "failed_layers": res.get("failed_layers", []),
                "not_cleared": res.get("not_cleared", []),
            }
            if outcome == "committed":
                return body
            if outcome == "partial":
                return JSONResponse(status_code=207, content=body)
            return JSONResponse(status_code=500, content=body)
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"delete_all 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    # v20.2.5（用户实测 Y-NEW3）：DELETE 方法别名。
    #
    # RESTful 惯例删除用 DELETE，集成方按惯例调 `DELETE /delete?memory_id=xxx`
    # 直接吃 405 —— 会以为接口坏了。
    #
    # 但**光加一个 `@app.delete` 装饰器是假修**：`DeleteRequest` 是 body 模型，
    # 而 DELETE 按惯例不带 body（httpx / requests 的 delete() 连 json= 参数都
    # 不给）。用户实机实测用的正是 query 参数。所以别名必须**收 query**，
    # 再转调同一个处理函数 —— 一份删除逻辑，两种调法。
    @app.delete("/delete")
    def delete_via_http_method(
        memory_id: str = Query(..., description="要删除的记忆 id"),
        user_id: str = Query(DEFAULT_USER_ID),
        bank_id: str = Query(DEFAULT_BANK_ID),
    ):
        """`DELETE /delete?memory_id=…` —— 与 `POST /delete` 行为逐字相同。"""
        return delete(DeleteRequest(memory_id=memory_id, user_id=user_id, bank_id=bank_id))

    # 🪦 tombstone 遗忘层（v19.4.0 Mímir 借鉴 B3）：遗忘不是删除，留痕可恢复
    @app.get("/tombstones")
    def tombstones(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID, limit: int = 50):
        """列某租户的遗忘记录（全文与撤回理由可查）"""
        try:
            from ducky.tombstone import list_tombstones
            scope = make_scope(user_id, bank_id)
            uid = _normalize_user_id(scope.user_id) if scope.user_id else DEFAULT_USER_ID
            return {"status": "ok", "user_id": uid, "bank_id": scope.bank_id, "results": list_tombstones(uid, limit=limit, bank_id=scope.bank_id)}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"tombstones 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    @app.post("/tombstone/restore")
    def tombstone_restore(req: TombstoneRestoreRequest):
        """从 tombstone 快照一键恢复一条记忆"""
        if not req.tombstone_id:
            raise HTTPException(400, "tombstone_id 不能为空")
        try:
            from ducky.tombstone import restore_tombstone
            scope = make_scope(req.user_id, req.bank_id)
            uid = _normalize_user_id(scope.user_id) if scope.user_id else DEFAULT_USER_ID
            res = restore_tombstone(req.tombstone_id, user_id=uid, bank_id=scope.bank_id)
            return {"status": "ok" if res.get("restored") else "noop", "details": res}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"tombstone/restore 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    # 📒 事件溯源账本（v19.4.0 Mímir 借鉴 B5）：任意记忆的完整变更史可查
    @app.get("/events/history")
    def events_history(target_id: str = "", limit: int = 100,
                       user_id: str = DEFAULT_USER_ID,
                       bank_id: str = DEFAULT_BANK_ID):
        """查某条记忆的完整变更史（谁、何时、做了什么、为什么）

        🟡-D（v19.4.1）：target_id 常常是自增整数，可被顺序枚举。
            宽松档（单机自托管默认）保持原行为；严格档
            （AIDUMEM_STRICT_TENANT=1）下校验该事实是否属本租户可见范围，
            不可见即当作不存在 —— 否则别处都收窄了，这条路由还敞着。
        """
        if not target_id or not target_id.strip():
            raise HTTPException(400, "target_id 不能为空")
        try:
            from ducky.event_ledger import get_history
            from ducky.facts_recall import fact_visible_to_tenant
            from ducky.utils import get_facts_conn

            # v20.2.4（外审 F-11）：**所有 target 形态走同一授权路径**。
            #
            # 此前只有 `bare.isdigit()` 才校验，于是 `fact:some-string-key`
            # 这类非数字键**完全绕过** fact_visible_to_tenant，且 get_history()
            # 调用时一个 scope 都不传 —— 严格档下攻击者照样拿到受害者的账本
            # 理由与操作者信息。
            #
            # 现在：数字键仍走归属校验（两轴，此前 bank 也没传）；**任何**形态
            # 都把 scope 交给 get_history —— 它自己就有 user_id/bank_id 参数，
            # 一直没人传。
            bare = target_id.strip()
            if bare.startswith("fact:"):
                bare = bare[5:]
            uid = _normalize_user_id(user_id) if user_id else DEFAULT_USER_ID
            if bare.isdigit():
                if not fact_visible_to_tenant(get_facts_conn(), int(bare), uid,
                                              bank_id=bank_id):
                    return {"status": "ok", "results": []}
            return {"status": "ok",
                    "results": get_history(target_id.strip(), limit=limit,
                                           user_id=uid, bank_id=bank_id)}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"events/history 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    # 🏛️ 治理管线（v19.4.0 Mímir 借鉴 B1）：候选队列 + 人审入口
    @app.get("/governance/candidates")
    def governance_candidates(status: str = "", user_id: str = "", limit: int = 50,
                              bank_id: str = "", scope_user_id: str = ""):
        """候选事实队列（可按状态过滤：pending/evaluated/approved/rejected/committed；
        v20：bank_id / scope_user_id 可选作用域过滤，不传保持全量视图）"""
        try:
            from ducky.governance import list_candidates
            return {"status": "ok", "results": list_candidates(
                status, user_id, limit, bank_id=bank_id, scope_user_id=scope_user_id)}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"governance/candidates 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    @app.post("/governance/review")
    def governance_review(req: GovernanceReviewRequest):
        """人审裁决：approve/reject 一条候选，带 reason 留痕"""
        if not req.candidate_id:
            raise HTTPException(400, "candidate_id 不能为空")
        if req.decision not in ("approve", "reject"):
            raise HTTPException(400, "decision 必须是 approve 或 reject")
        try:
            from ducky.governance import review_candidate
            # v20 P0-2：只有调用方显式声明了 bank_id 才启用越库裁决守卫——
            # 模型字段有 DEFAULT_BANK_ID 缺省值，无脑透传会把「没传 bank 的
            # 管理员全权裁决」误判成「default 库越权」，v19 存量调用全断。
            explicit_bank = req.bank_id if "bank_id" in req.model_fields_set else ""
            res = review_candidate(req.candidate_id, req.decision,
                                   reason=req.reason, user_id=req.user_id,
                                   bank_id=explicit_bank)
            return {"status": "ok", "details": res}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"governance/review 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    # 🧭 信念层 Opinion（v19.4.0 Mímir 借鉴 B6）：三态信念写入 + 聚合判定
    @app.post("/opinions/set")
    def opinion_set(req: OpinionSetRequest):
        """写入一条信念（support/oppose/neutral 三态皆可），账本留痕"""
        if not req.fact_id:
            raise HTTPException(400, "fact_id 不能为空")
        if not req.source or not req.source.strip():
            raise HTTPException(400, "source（证据来源标识）不能为空")
        try:
            from ducky.opinion import set_opinion
            res = set_opinion(req.fact_id, req.stance, confidence=req.confidence,
                              evidence_ids=req.evidence_ids, source=req.source,
                              owner=req.owner)
            return {"status": "ok" if res.get("ok") else "error", "details": res}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"opinions/set 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    @app.get("/opinions")
    def opinions_list(fact_id: int = 0, user_id: str = DEFAULT_USER_ID,
                      # v20.2.4（外审 F-11）：此前**没有 bank 参数**，
                      # 可见性校验一律按默认域判 —— 具名域的信念对谁都可见。
                      bank_id: str = DEFAULT_BANK_ID):
        """查某事实的信念清单（严格档下按租户可见性校验，见 🟡-D）"""
        if not fact_id:
            raise HTTPException(400, "fact_id 不能为空")
        try:
            from ducky.facts_recall import fact_visible_to_tenant
            from ducky.opinion import list_opinions
            from ducky.utils import get_facts_conn

            uid = _normalize_user_id(user_id) if user_id else DEFAULT_USER_ID
            if not fact_visible_to_tenant(get_facts_conn(), fact_id, uid,
                                          bank_id=bank_id):
                return {"status": "ok", "results": []}
            return {"status": "ok", "results": list_opinions(fact_id)}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"opinions 查询失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    @app.get("/opinions/aggregate")
    def opinions_aggregate(fact_id: int = 0, user_id: str = DEFAULT_USER_ID,
                           bank_id: str = DEFAULT_BANK_ID):
        """聚合判定：≥2 个不同证据来源才聚合（单来源刷好评不聚合）"""
        if not fact_id:
            raise HTTPException(400, "fact_id 不能为空")
        try:
            from ducky.facts_recall import fact_visible_to_tenant
            from ducky.opinion import aggregate_opinion
            from ducky.utils import get_facts_conn

            uid = _normalize_user_id(user_id) if user_id else DEFAULT_USER_ID
            if not fact_visible_to_tenant(get_facts_conn(), fact_id, uid,
                                          bank_id=bank_id):
                return {"status": "ok", "details": {}}
            return {"status": "ok", "details": aggregate_opinion(fact_id)}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"opinions/aggregate 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    @app.post("/update")
    def update(req: UpdateRequest):
        # 🔴P0-4: 传递并严格校验 user_id 归属，并同步更新 FTS5、facts 与 memory_types
        if not req.memory_id or not req.memory_id.strip():
            raise HTTPException(400, "memory_id 不能为空")
        try:
            mem = get_memory()
            content = req.content
            if not content:
                extra = getattr(req, "model_extra", None) or {}
                content = extra.get("data", "")
            
            scope = make_scope(req.user_id, req.bank_id)
            user_id = _normalize_user_id(scope.user_id) if scope.user_id else DEFAULT_USER_ID
            # /update 会把 bank_id 盖进向量 metadata 并按该域重建 FTS 索引，
            # 也就是说它能把一条记忆搬进一个从没被注册过的域。写路径里只有
            # 这一处漏了注册（add / tombstone / core_memory / conflict_resolver
            # 都调了），结果是数据落在某域、memory_banks 里却查不到这个域 ——
            # 域存在与否取决于当初是从哪个端点进来的，注册表从此不可信。
            # INSERT OR IGNORE，幂等，对已注册域是 no-op。
            ensure_bank_registered(scope)
            mem.update(req.memory_id, data=content, metadata={"bank_id": scope.bank_id})
            
            # 同步更新 FTS
            try:
                from ducky.text_fts import _index_memory
                _index_memory(req.memory_id, content, user_id=user_id, bank_id=scope.bank_id)
            except Exception as fe:
                feature_failed("index_memory", fe)
                logger.debug(f"FTS index on update 跳过: {fe}")

            # 同步更新 facts.db 事实内容与更新时间
            try:
                from ducky.utils import get_facts_conn
                fconn = get_facts_conn()
                if user_id == DEFAULT_USER_ID:
                    fconn.execute(
                        "UPDATE facts SET fact_value=?, updated_at=CURRENT_TIMESTAMP WHERE (id=? OR fact_key=?) AND user_id=? AND bank_id=?",
                        (content, req.memory_id, req.memory_id, user_id, scope.bank_id),
                    )
                else:
                    fconn.execute(
                        "UPDATE facts SET fact_value=?, updated_at=CURRENT_TIMESTAMP WHERE (id=? OR fact_key=?) AND user_id=? AND bank_id=?",
                        (content, req.memory_id, req.memory_id, user_id, scope.bank_id),
                    )
                fconn.commit()
                fconn.close()
            except Exception as fte:
                logger.debug(f"facts update on update 跳过: {fte}")

            return {"status": "ok"}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            feature_failed("index_memory", e)
            logger.error(f"update 失败: {e}")
            raise HTTPException(500, api_error_detail(e))

    @app.get("/usage")
    def usage(start: str = None, end: str = None):
        local_usage = get_llm_usage()
        try:
            from ducky.router_usage import fetch_router_llm_usage
            router_llm = fetch_router_llm_usage()
            if router_llm:
                merged = dict(local_usage) if local_usage else {}
                for dt, info in router_llm.items():
                    if dt not in merged:
                        merged[dt] = {}
                    merged[dt]["llm"] = info
                return {"status": "ok", "source": "router", "usage": merged}
        except Exception as e:
            logger.warning(f"获取上游网关用量失败，回退到本地: {e}")
        return {"status": "ok", "source": "local", "usage": local_usage}

    @app.post("/reload")
    def reload_mem0():
        reset_memory_singleton()
        try:
            _ = get_memory()
            return {"status": "ok", "message": "mem0 重新加载成功"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _do_inject_context(req: InjectContextRequest) -> dict:
        from ducky.facts_recall import inject_context as inject_facts_context
        # 🔴P0-2（v19.4.1）：注入上下文按租户收窄 —— 注入是记忆流向宿主
        # 模型的出口，此处漏租户等于把别人的事实喂进本租户的对话。
        # InjectContextRequest 早已带 user_id 字段，此前未透传。
        return inject_facts_context(
            req.query,
            k=req.k,
            level=req.level,
            max_tokens=req.max_tokens,
            user_id=req.user_id,
            bank_id=req.bank_id,
        )

    @app.post("/facts/inject-context")
    def inject_context(req: dict):
        return _do_inject_context(InjectContextRequest(**req))

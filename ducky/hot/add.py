"""ducky.hot.add — POST /add + job/coalesce 运维端点"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException

from ducky.api_models import AddRequest
from ducky.mem0_runtime import (
    _normalize_user_id,
    get_memory,
    lazy_import_layer1,
    register_salience_for_add,
)
from ducky.bank_contract import ensure_bank_registered, make_scope
from ducky.failure_ledger import feature_failed

logger = logging.getLogger("aiduMEM.hot")


def register_add_routes(app: FastAPI) -> None:
    @app.post("/add")
    def add(req: AddRequest, background_tasks: BackgroundTasks = None):
        """写入记忆 — 高速路径：计时 / 快路径 / 缓存 / 会话合并 / 可选异步回执

        async_mode=true（或 body 里 "async": true）时：
          立刻返回 accepted + job_id，后台完成 LLM 抽取落库。
        短句连发（async）：进入 coalesce 队列，idle/window 到后合并一次 LLM。
        默认同步：完整抽取后返回，兼容旧调用方。
        """
        try:
            # 🔴P1-4: 统一规范化 user_id，杜绝脏租户与空租户写入
            scope = make_scope(req.user_id, req.bank_id)
            req.user_id = _normalize_user_id(scope.user_id) if scope.user_id else "default"
            req.bank_id = scope.bank_id
            # v20.1.1（N-1）：写路径限流——拦失控循环，不拦正常流量
            # （默认 120/min，生产 14 天分钟峰值 35 的 3.4 倍）。
            from ducky.rate_guard import add_rate_limit, check_rate
            _retry = check_rate("add", req.user_id, limit=add_rate_limit())
            if _retry is not None:
                raise HTTPException(
                    status_code=429,
                    detail=f"写入频率超限（租户 {req.user_id}）：默认护栏用于拦截失控循环，"
                           f"{_retry}s 后重试；上限可经 AIDUMEI_RATE_ADD_PER_MIN 调整（0=关闭）",
                    headers={"Retry-After": str(_retry)},
                )
            ensure_bank_registered(make_scope(req.user_id, req.bank_id))

            from ducky.add_speed import (
                coalesce_enqueue,
                coalesce_should_buffer,
                ensure_coalesce_worker,
                job_create,
                job_update,
                load_speed_cfg,
                messages_to_text,
                patch_llm_for_speed,
                register_coalesce_flusher,
            )

            mem = get_memory()
            patch_llm_for_speed(mem)

            # 解析 messages
            if isinstance(req.messages, str):
                try:
                    messages_json = json.loads(req.messages) if req.messages.strip().startswith(("[", "{")) else req.messages
                except ValueError:
                    # 收窄自 except Exception：这里唯一可能抛的是 json 解析失败
                    # （JSONDecodeError 继承 ValueError）。原来的宽捕获会连带
                    # 吞掉 MemoryError / KeyboardInterrupt 之外的一切系统级异常，
                    # 让「进程出事」伪装成「这串文本不是 JSON」。降级本身是对的，
                    # 不是 JSON 就按纯文本存，所以无需记录。
                    messages_json = req.messages
            else:
                messages_json = req.messages

            # 兼容 async / async_mode
            async_flag = bool(getattr(req, "async_mode", False))
            extra = getattr(req, "__pydantic_extra__", None) or {}
            # v20：免抽取写入开关。默认 True＝生产语义不变。
            infer_flag = bool(getattr(req, "infer", True))
            if not async_flag and isinstance(extra, dict):
                async_flag = bool(extra.get("async") or extra.get("async_mode"))
            # metadata 里也可带 async
            md = dict(req.metadata or {})
            # v20: bank scope is carried as explicit metadata for mem0/Qdrant
            # payloads and for every downstream storage layer.  It is never
            # inferred from a table name or a free-form user string.
            md.setdefault("bank_id", req.bank_id)

            # P0-1 写入侧自动时间戳（与生产环境对齐）：
            # 调用方未显式传 recorded_at 时自动补 UTC ISO 时间，供
            # 时间过滤（before/after）和三级时间戳回退使用。
            # 注意：仅新写入路径需要该时间戳。md 会被透传给 memory.add
            # 与 self-edit 合并路径的 memory.update；若在这里统一补上，
            # 合并会把旧记忆的 recorded_at 覆盖成「现在」，破坏 before/
            # after 时间推理。因此只在新增落库时补，合并不受影响。
            md.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())

            # --- 🚀 v18.3 核心：原生多模态支持 (Phase 2) ---
            media_url = md.get("media_url") or md.get("image_url") or (extra.get("media_url") if isinstance(extra, dict) else None)
            vision_caption = ""
            if media_url:
                # 检查 Vision 模块开关
                try:
                    import os
                    from ducky.mem0_runtime import MEM0_CONFIG
                    if os.path.exists(MEM0_CONFIG):
                        with open(MEM0_CONFIG) as f:
                            _cfg = json.loads(f.read())
                            _features = _cfg.get("_features", {})
                            if not _features.get("vision", True):
                                logger.info("多模态 Vision 模块已禁用，跳过解析")
                                media_url = None  # 置空，走普通文本路径
                except Exception as _fe:
                    logger.warning(f"读取 features 配置失败: {_fe}")

            if media_url:
                try:
                    from ducky.pipeline.memory_vision import extract_vision_caption
                    vision_caption = extract_vision_caption(media_url)
                    # 🟢23：vision 失败时返回以「图片解析失败」开头的字符串（非异常），
                    # 此前被无条件当作正常 caption 拼进 messages 落库，让失败信息变成一条记忆。
                    # 这里显式识别失败前缀，失败则不拼接、不回写 metadata。
                    if vision_caption and not vision_caption.startswith("图片解析失败"):
                        logger.info(f"成功提取多模态 Caption: {vision_caption[:50]}...")
                        # 把 caption 合并到 messages 供下游写入 DB
                        if isinstance(messages_json, list):
                            messages_json.append({"role": "user", "content": f"[附带多模态分析]: {vision_caption}"})
                        elif isinstance(messages_json, dict):
                            messages_json["content"] = f"{messages_json.get('content', '')}\n[附带多模态分析]: {vision_caption}"
                        else:
                            messages_json = f"{messages_json}\n[附带多模态分析]: {vision_caption}"

                        # 回写 metadata，以便底层能够存到新的 DB column
                        md["media_url"] = media_url
                        md["vision_caption"] = vision_caption
                    else:
                        logger.warning(f"多模态解析失败，跳过 caption 落库: {vision_caption}")
                except Exception as ve:
                    logger.error(f"多模态提取失败: {ve}")
            # ---------------------------------------------

            if not async_flag:
                async_flag = bool(md.pop("async", False) or md.pop("async_mode", False))

            # 2026-07-21：Hermes/飞书写入默认异步（体感起飞）
            # - force_sync=true 可强制同步
            # - async_sources / hermes category 自动 async
            # - async_default=true 时全局默认异步（仍可 force_sync 关掉）
            speed_cfg = load_speed_cfg()
            force_sync = bool(md.pop("force_sync", False) is True or md.get("sync") is True)
            if force_sync:
                async_flag = False
            elif not async_flag:
                src = str(md.get("source") or md.get("caller") or "").lower()
                cat = str(md.get("category") or "").lower()
                auto_sources = {
                    str(x).lower()
                    for x in (speed_cfg.get("async_sources") or [
                        "mem0_sync", "hermes", "hermes_memory",
                        "chat", "auto_memory", "memory_md",
                        "memory_trim", "user_trim", "cron", "cron_lesson", "state_archive",
                    ])
                }
                # 亲密/日记类 category 也默认异步（才能进 coalesce intimate）
                intimate_cats = {
                    str(k).lower()
                    for k, v in (speed_cfg.get("coalesce_profile_by_category") or {}).items()
                    if str(v).lower() == "intimate"
                }
                if (
                    src in auto_sources
                    or cat in auto_sources
                    or cat == "hermes_memory"
                    or cat in intimate_cats
                ):
                    async_flag = True
                elif bool(speed_cfg.get("async_default")):
                    async_flag = True

            text_preview = messages_to_text(messages_json)[:120]
            from ducky.security.injection_guard import validate_and_sanitize_memory_content
            _full_text = messages_to_text(messages_json)
            _is_safe, _, _rejection = validate_and_sanitize_memory_content(_full_text)
            if not _is_safe:
                logger.warning(f"🛡️ [InjectionGuard] POST /add 拦截注入: {_rejection}")
                raise HTTPException(400, f"Memory content rejected: {_rejection}")

            # 📼 v19.4.0 明镜工程 Phase 1: Verbatim Vault 原文保真层
            # 注入防御通过后，把逐字原文并行落库（mem0 抽取之外的第二层）。
            # 幂等去重 + 失败干净降级，绝不阻断主链路。
            try:
                from ducky.verbatim_vault import store_verbatim
                store_verbatim(req.user_id, messages_json, md, bank_id=req.bank_id)
            except Exception as _ve:
                feature_failed("store_verbatim", _ve)
                logger.debug(f"📼 [VerbatimVault] 原文落库跳过: {_ve}")

            # 🐙 v16.0 Opus Octopod (opus八爪鱼): 写入前触发隐式冲突检测与消解
            try:
                from ducky.conflict_resolver import scan_and_resolve_text_conflicts
                scan_and_resolve_text_conflicts(text_preview, user_id=req.user_id, bank_id=req.bank_id)
            except Exception as _ce:
                logger.warning(f"🐙 [ConflictResolver] 隐式检测异常: {_ce}")

            # 🧩 v20.1 WP-A: 确定性抽取层 —— LLM 之外的第二事实来源。
            # 放在路由层（异步分发之前）：同步 / async job / coalesce 三条
            # 通路都必然经过这里，且看到的是合并前的原始请求文本。
            # LLM 空返回时，日期/版本/指令/偏好等硬事实仍有确定性通路落
            # facts 层（source='pattern_extract'，可按来源精确清除）。
            # 失败进 failure_ledger，绝不阻断主链路。
            try:
                from ducky.pattern_extract import extract_and_store
                extract_and_store(_full_text, user_id=req.user_id,
                                  bank_id=req.bank_id,
                                  recorded_at=md.get("recorded_at"))
            except Exception as _pe:
                feature_failed("pattern_extract", _pe)
                logger.warning(f"🧩 [PatternExtract] 确定性抽取跳过: {_pe}")

            # 🪫 v20.2 自动挡（WP-F/WP-H）：
            # ① 原文本地向量 —— lite 挡语义召回的语料，路由层单点写入
            #    （三条通路全覆盖，与 pattern_extract 同位置哲学），软失败
            #    进欠账绝不阻断。
            # ② lite 挡分流 —— 云嵌入熔断期间完全绕开 mem0 主体（LLM 蒸馏
            #    + 云向量整笔进欠账，升挡后重放走完整管线），确定性层
            #    （pattern facts / verbatim / 本地向量）已在上方全部落完。
            #    挡位如实回给调用方，蒸馏延迟不装没事。
            try:
                from ducky.dual_index import upsert_local_verbatim
                upsert_local_verbatim(req.user_id, req.bank_id, _full_text)
            except Exception as _lv:
                feature_failed("dual_index_local", _lv)
                logger.debug(f"🪫 [DualIndex] 原文本地向量跳过: {_lv}")
            try:
                from ducky.engine_mode import cloud_leg_enabled
                from ducky.gear import current_mode
                if not cloud_leg_enabled():
                    # 🔋 本地档（v20.2.3）：零 token、零外部网络。确定性抽取、
                    # 原文、本地向量已在上方全部落完，云侧**不入欠账** ——
                    # 欠账的语义是「等恢复后补算」，而本地档没有「恢复」
                    # 这回事（是部署方的选择，不是故障）。入了就是永不清零
                    # 的假水位，会把 /health 的欠账探针变成噪声。
                    return {
                        "status": "ok",
                        "action": "local_only",
                        "engine_mode": "local",
                        "detail": "本地档：硬事实、原文与本地向量已落库并可召回；"
                                  "按部署配置不调用云端 LLM 与云嵌入（零 token）",
                    }
                if current_mode() == "lite":
                    from ducky.dual_index import enqueue_cloud_add
                    enqueue_cloud_add(
                        {"messages": req.messages if isinstance(req.messages, str)
                         else messages_json, "metadata": dict(md)},
                        req.user_id, req.bank_id)
                    return {
                        "status": "ok",
                        "action": "deferred_distillation",
                        "engine_mode": "lite",
                        "detail": "云嵌入熔断中：硬事实与原文已确定性落库并可召回；"
                                  "LLM 蒸馏与云向量已入欠账，服务恢复后自动补算",
                    }
            except HTTPException:
                raise
            except Exception as _ge:
                logger.warning(f"⚙️ [Gear] 挡位分流异常（回落 full 路径）: {_ge}")

            def _direct_write(uid, msgs, meta, infer_effective, note=None):
                """确定性直写（layer1 之外的兜底通路）。infer_effective 由
                调用方决定：非 LLM 故障透传调用方的 infer（v20 纪律——显式
                免抽取不许偷偷变回 LLM 抽取）；LLM 故障/挡位 open 强制
                False（否则 fallback 里还藏着一次 mem0 内部 LLM 调用 ——
                2026-08-26 实弹里网关恰好复活才没暴露的洞）。"""
                try:
                    add_result = mem.add(msgs, user_id=uid, metadata=meta,
                                         infer=infer_effective)
                except Exception as _de:
                    # fallback 自身纯化：直写内层再撞 LLMError（infer=True
                    # 且 LLM 恰在此刻死）→ 上报挡位并就地降为 infer=False，
                    # 绝不让 fallback 自己 500。非 LLM 异常照旧上抛。
                    if type(_de).__name__ != "LLMError":
                        raise
                    from ducky.gear import record_llm_failure
                    record_llm_failure(str(_de))
                    logger.warning(f"直写内层 LLM 失败，就地降为确定性直写: {_de}")
                    note = note or "skipped_llm_error"
                    add_result = mem.add(msgs, user_id=uid, metadata=meta,
                                         infer=False)
                register_salience_for_add(add_result, user_id=uid, bank_id=req.bank_id)
                try:
                    from ducky.text_fts import _index_memory
                    results = add_result if isinstance(add_result, list) else (add_result.get("results") if isinstance(add_result, dict) else [])
                    if isinstance(results, list):
                        for r in results:
                            if not isinstance(r, dict):
                                continue
                            mid = r.get("id") or r.get("memory_id")
                            content = r.get("memory") or r.get("data") or ""
                            if mid and content:
                                # 🔴v20：降级路径曾漏传 bank_id——向量进了
                                # work 域、FTS 行落在 default 域，命名域的
                                # 关键词召回永远查不到这条。此处必须带域。
                                _index_memory(mid, content, user_id=uid, category=(meta or {}).get("category"), bank_id=req.bank_id)
                except Exception as ie:
                    feature_failed("index_memory", ie)
                    logger.debug(f"FTS index on add 跳过: {ie}")
                out = {"status": "ok", "action": "direct"}
                if note:
                    # 诚实注记（additive，不动既有契约）：这条写入没做蒸馏。
                    out["distillation"] = note
                return out

            def _run_pipeline(uid, msgs, meta, *, bank_id=None):
                # v20.2.4（外审 F-04）：bank 走**参数**，默认回退闭包里的
                # req.bank_id（同请求内的同步调用行为不变）。跨请求的 coalesce
                # 冲刷必须显式传 batch 自带的 scope —— 全局回调是进程级单例，
                # 闭包里那个 req 属于「最后一次注册的请求」。
                # ⚙️ v20.2.2 LLM 腿挡位：open 态不再逐请求撞超时——直接
                # 确定性直写秒回（原文/硬事实/云向量照落，内容照样可召回；
                # 欠的只是蒸馏精修，故障账本与事件账本可查）。closed/half-open
                # 走真实蒸馏，半开拿真实写入当探针（命门教训）。
                from ducky.gear import record_llm_failure, record_llm_success, should_try_llm
                try:
                    _try_llm = should_try_llm()
                except Exception:
                    _try_llm = True
                if not _try_llm and infer_flag:
                    return _direct_write(uid, msgs, meta, False,
                                         note="skipped_llm_gear_open")
                try:
                    _r = lazy_import_layer1()(
                        mem, msgs, uid, meta,
                        bank_id=(bank_id or req.bank_id), infer=infer_flag,
                    )
                    # 成功信号只在 LLM 真被使用过时上报（infer=False 的
                    # layer1 整段跳过 LLM——记成功就是假信号）。
                    if infer_flag:
                        record_llm_success()
                    return _r
                except Exception as e:   # P2-5（v19.4.1）：ImportError 是 Exception 子类，元组冗余
                    feature_failed("index_memory", e)
                    logger.warning(f"Layer 1 自检异常，降级为直接写入: {e}")
                    # 信号纯净（Y2 教训的写侧版）：只有 LLMError 形态计入
                    # LLM 腿；FTS/salience 等非 LLM 崩溃不许污染挡位。
                    if type(e).__name__ == "LLMError":
                        record_llm_failure(str(e))
                        return _direct_write(uid, msgs, meta, False,
                                             note="skipped_llm_error")
                    # v20：非 LLM 故障的降级分支照旧尊重 infer —— 否则
                    # 调用方显式要的免抽取写入会在降级时偷偷变回 LLM 抽取，
                    # 确定性通路就成了「大部分时候确定」。
                    return _direct_write(uid, msgs, meta, infer_flag)

            def _execute_batch(uid, msgs, meta, job_ids, *, bank_id=None):
                """合并包 / 单条异步包统一执行，并把结果回写到所有关联 job。"""
                jids = list(job_ids or [])
                for jid in jids:
                    job_update(jid, status="running")
                try:
                    result = _run_pipeline(uid, msgs, meta or {}, bank_id=bank_id)
                    # 标注 coalesce 信息到 result.details
                    if isinstance(result, dict):
                        details = dict(result.get("details") or {})
                        if (meta or {}).get("coalesced"):
                            details["coalesced"] = True
                            details["coalesce_count"] = (meta or {}).get("coalesce_count")
                            details["coalesce_reason"] = (meta or {}).get("coalesce_reason")
                            details["coalesce_profile"] = (meta or {}).get("coalesce_profile")
                            result = {**result, "details": details}
                    payload = {"status": "done", "result": result}
                    if jids:
                        primary, *rest = jids
                        job_update(primary, **payload)
                        for jid in rest:
                            job_update(
                                jid,
                                status="done",
                                result={
                                    **(result if isinstance(result, dict) else {"status": "ok"}),
                                    "coalesce_follower": True,
                                    "primary_job_id": primary,
                                },
                            )
                    return result
                except Exception as be:
                    logger.error(f"add batch failed jobs={jids}: {be}")
                    for jid in jids:
                        job_update(jid, status="error", error=str(be)[:300])
                    raise

            # 注册 coalesce 冲刷回调 + 后台 worker（只一次）
            def _coalesce_cb(uid, msgs, meta, job_ids, *, bank_id="default"):
                # v20.2.4（F-04）：scope 从 batch 参数来，**不读闭包里的 req**
                _execute_batch(uid, msgs, meta, job_ids, bank_id=bank_id)

            register_coalesce_flusher(_coalesce_cb)
            ensure_coalesce_worker()

            # ── 异步路径 ──
            if async_flag and background_tasks is not None:
                job_id = job_create({"text_preview": text_preview, "user_id": req.user_id})

                # 短句连发 → 合并队列（省 LLM）
                should, why = coalesce_should_buffer(
                    req.user_id, messages_json, md, async_mode=True
                )
                if should:
                    enq = coalesce_enqueue(
                        req.user_id, messages_json, md, job_id=job_id,
                        bank_id=req.bank_id,          # F-04：scope 随 batch 落库
                    )
                    # 若顺带带出已到期的旧包 / 满额包，立刻后台执行
                    batches = []
                    if enq.get("merged_ready") and enq.get("messages"):
                        batches.append({
                            "user_id": enq.get("user_id") or req.user_id,
                            "messages": enq["messages"],
                            "metadata": enq.get("metadata") or md,
                            "job_ids": enq.get("job_ids") or [job_id],
                        })
                    for extra_batch in (enq.get("also_ready") or []):
                        batches.append(extra_batch)

                    for b in batches:
                        background_tasks.add_task(
                            _execute_batch,
                            b["user_id"],
                            b["messages"],
                            b.get("metadata") or {},
                            b.get("job_ids") or [],
                        )

                    if enq.get("buffered"):
                        job_update(
                            job_id,
                            status="coalescing",
                            result={
                                "status": "coalescing",
                                "action": "coalesce_buffered",
                                "count": enq.get("count"),
                                "key": enq.get("key"),
                                "profile": enq.get("profile"),
                                "idle_sec": enq.get("idle_sec"),
                                "window_sec": enq.get("window_sec"),
                            },
                        )
                        return {
                            "status": "accepted",
                            "action": "coalesce_buffered",
                            "job_id": job_id,
                            "infer": infer_flag,
                            "message": "短句已入合并队列，空闲后一次总结落库",
                            "preview": text_preview,
                            "coalesce": {
                                "count": enq.get("count"),
                                "key": enq.get("key"),
                                "profile": enq.get("profile"),
                                "idle_sec": enq.get("idle_sec"),
                                "window_sec": enq.get("window_sec"),
                            },
                        }
                    # 当前句触发了满额即时冲刷
                    return {
                        "status": "accepted",
                        "action": "coalesce_flushed",
                        "job_id": job_id,
                        "infer": infer_flag,
                        "message": "合并包已提交后台总结落库",
                        "preview": text_preview,
                        "coalesce": {
                            "count": enq.get("count"),
                            "reason": enq.get("flush_reason"),
                            "key": enq.get("key"),
                            "profile": enq.get("profile"),
                        },
                    }

                # 不进合并：单条异步
                def _bg_job(jid=job_id, msgs=messages_json, meta=md, uid=req.user_id):
                    _execute_batch(uid, msgs, meta, [jid])

                background_tasks.add_task(_bg_job)
                return {
                    "status": "accepted",
                    "action": "async_queued",
                    "job_id": job_id,
                    "infer": infer_flag,
                    "message": "已收下，后台正在总结落库",
                    "preview": text_preview,
                    "coalesce_skip": why,
                }

            out = _run_pipeline(req.user_id, messages_json, md)
            # v20：回显 infer —— 调用方（尤其跑分适配器）据此断言服务端
            # 真的按免抽取写入执行了，而不是把这个字段静默丢掉。
            if isinstance(out, dict):
                out = {**out, "infer": infer_flag}
            return out
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            feature_failed("index_memory", e)
            feature_failed("store_verbatim", e)
            logger.error(f"add 失败: {e}")
            raise HTTPException(500, str(e))

    @app.get("/add/job/{job_id}")
    def add_job_status(job_id: str):
        """查询异步 /add 任务状态"""
        try:
            from ducky.add_speed import job_get
            rec = job_get(job_id)
            if not rec:
                raise HTTPException(404, f"job not found: {job_id}")
            return {"status": "ok", "job": rec}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.get("/add/coalesce")
    def add_coalesce_status(user_id: str = ""):
        """查看会话合并队列水位（调试/运维）"""
        try:
            from ducky.add_speed import coalesce_status, ensure_coalesce_worker
            ensure_coalesce_worker()
            return {"status": "ok", **coalesce_status(user_id or None)}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.get("/add/coalesce/stats")
    def add_coalesce_stats(reset: bool = False):
        """潮浪命中统计：waves / saved_llm / by_profile / last_waves。reset=true 清零。"""
        try:
            from ducky.add_speed import coalesce_stats_snapshot
            return {"status": "ok", **coalesce_stats_snapshot(reset=reset)}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/add/coalesce/flush")
    def add_coalesce_flush(user_id: str = "", force: bool = True,
                           # v20.2.4（外审 F-04）：手动冲刷也必须接受作用域 ——
                           # 否则一个域的 preview 会被交给另一个域处理。
                           bank_id: str = ""):
        """手动冲刷合并队列（调试）"""
        try:
            from ducky.add_speed import coalesce_flush_due, ensure_coalesce_worker
            from ducky.add_speed import job_update, patch_llm_for_speed

            mem = get_memory()
            patch_llm_for_speed(mem)

            def _run_pipeline(uid, msgs, meta, *, bank_id="default"):
                # F-04：bank 从 batch 自带的 scope 来，不猜、不读全局
                return lazy_import_layer1()(mem, msgs, uid, meta or {}, bank_id=bank_id)

            flushed = []
            batches = coalesce_flush_due(user_id=(user_id or None), force=force,
                                         bank_id=(bank_id or None))
            for b in batches:
                jids = b.get("job_ids") or []
                for jid in jids:
                    job_update(jid, status="running")
                try:
                    result = _run_pipeline(b["user_id"], b["messages"], b.get("metadata") or {},
                                           bank_id=b.get("bank_id") or "default")
                    for jid in jids:
                        job_update(jid, status="done", result=result)
                    flushed.append({
                        "key": b.get("key"),
                        "count": b.get("count"),
                        "reason": b.get("reason"),
                        "job_ids": jids,
                        "action": (result or {}).get("action") if isinstance(result, dict) else None,
                    })
                except Exception as e:
                    for jid in jids:
                        job_update(jid, status="error", error=str(e)[:300])
                    flushed.append({"key": b.get("key"), "error": str(e)[:200]})
            ensure_coalesce_worker()
            return {"status": "ok", "flushed": flushed, "n": len(flushed)}
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))

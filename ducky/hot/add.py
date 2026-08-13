"""ducky.hot.add — POST /add + job/coalesce 运维端点"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException

from ducky.api_models import AddRequest
from ducky.mem0_runtime import (
    get_memory,
    lazy_import_layer1,
    register_salience_for_add,
)

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
                except Exception:
                    messages_json = req.messages
            else:
                messages_json = req.messages

            # 兼容 async / async_mode
            async_flag = bool(getattr(req, "async_mode", False))
            extra = getattr(req, "__pydantic_extra__", None) or {}
            if not async_flag and isinstance(extra, dict):
                async_flag = bool(extra.get("async") or extra.get("async_mode"))
            # metadata 里也可带 async
            md = dict(req.metadata or {})

            # P0-1 写入侧自动时间戳（与生产对齐）：
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
                    import json
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

            # 🐙 v16.0 Opus Octopod (opus八爪鱼): 写入前触发隐式冲突检测与消解
            try:
                from ducky.conflict_resolver import scan_and_resolve_text_conflicts
                scan_and_resolve_text_conflicts(text_preview, user_id=req.user_id)
            except Exception as _ce:
                logger.warning(f"🐙 [ConflictResolver] 隐式检测异常: {_ce}")

            def _run_pipeline(uid, msgs, meta):
                try:
                    return lazy_import_layer1()(mem, msgs, uid, meta)
                except (ImportError, Exception) as e:
                    logger.warning(f"Layer 1 自检异常，降级为直接写入: {e}")
                    add_result = mem.add(msgs, user_id=uid, metadata=meta)
                    register_salience_for_add(add_result)
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
                                    _index_memory(mid, content, user_id=uid, category=(meta or {}).get("category", ""))
                    except Exception as ie:
                        logger.debug(f"FTS index on add 跳过: {ie}")
                    return {"status": "ok", "action": "direct"}

            def _execute_batch(uid, msgs, meta, job_ids):
                """合并包 / 单条异步包统一执行，并把结果回写到所有关联 job。"""
                jids = list(job_ids or [])
                for jid in jids:
                    job_update(jid, status="running")
                try:
                    result = _run_pipeline(uid, msgs, meta or {})
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
            def _coalesce_cb(uid, msgs, meta, job_ids):
                _execute_batch(uid, msgs, meta, job_ids)

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
                        req.user_id, messages_json, md, job_id=job_id
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
                    "message": "已收下，后台正在总结落库",
                    "preview": text_preview,
                    "coalesce_skip": why,
                }

            return _run_pipeline(req.user_id, messages_json, md)
        except Exception as e:
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
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.get("/add/coalesce/stats")
    def add_coalesce_stats(reset: bool = False):
        """潮浪命中统计：waves / saved_llm / by_profile / last_waves。reset=true 清零。"""
        try:
            from ducky.add_speed import coalesce_stats_snapshot
            return {"status": "ok", **coalesce_stats_snapshot(reset=reset)}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/add/coalesce/flush")
    def add_coalesce_flush(user_id: str = "", force: bool = True):
        """手动冲刷合并队列（调试）"""
        try:
            from ducky.add_speed import coalesce_flush_due, ensure_coalesce_worker
            from ducky.add_speed import job_update, patch_llm_for_speed

            mem = get_memory()
            patch_llm_for_speed(mem)

            def _run_pipeline(uid, msgs, meta):
                return lazy_import_layer1()(mem, msgs, uid, meta or {})

            flushed = []
            batches = coalesce_flush_due(user_id=(user_id or None), force=force)
            for b in batches:
                jids = b.get("job_ids") or []
                for jid in jids:
                    job_update(jid, status="running")
                try:
                    result = _run_pipeline(b["user_id"], b["messages"], b.get("metadata") or {})
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
        except Exception as e:
            raise HTTPException(500, str(e))


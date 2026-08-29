"""ducky.hot.search — /search /search_trace"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException

from ducky.api_models import SearchRequest, SearchResponse
from ducky.mem0_runtime import (
    _normalize_user_id,
    boost_salience_for_results,
    get_memory,
    lazy_import_funnel,
    lazy_import_hybrid,
)
from ducky.bank_contract import (
    ensure_bank_registered,
    make_scope,
    vector_item_in_bank,
    vector_scope_filters,
)

logger = logging.getLogger("aiduMEM.hot")


def _annotate_memory_types(results: list, *, user_id: str = "default",
                           bank_id: str = "default") -> None:
    """把六型分类结果回填到检索结果（P2-3 / v19.2.0：单次 SQL 批量加载，消除 N+1 读查询）。

    - 只读账本，单次 SQL 批量加载，不触发任何 LLM 调用，检索性能不受影响；
    - 每条结果写入 memory_type 字段；账本无记录时默认 FACTS。
    - ref 命中优先级：mem0 UUID（主链写时）→ fact:{fact_id}（backfill 写时）。
    - 失败静默降级（检索优先，分类失败不阻断召回）。
    """
    if not results:
        return
    try:
        # v20.2.4（外审 F-15）：键构造收敛到 memory_type_ref（此前这里和 scoring
        # 各写一份，规则不同），查询带上 scope（此前用默认 scope，命名 bank 查不到）。
        from ducky.memory_types import get_batch_memory_types, memory_type_ref

        ref_list = []
        item_ref_map = []
        for item in results:
            if not isinstance(item, dict):
                continue
            ref = memory_type_ref(item)
            if ref:
                ref_str = str(ref)
                ref_list.append(ref_str)
                item_ref_map.append((item, ref_str))
            else:
                item["memory_type"] = "FACTS"

        if ref_list:
            type_map = get_batch_memory_types(ref_list, user_id=user_id, bank_id=bank_id)
            for item, ref_str in item_ref_map:
                item["memory_type"] = type_map.get(ref_str, "FACTS")
    except Exception:
        return


def _apply_time_window_to_trace(result: dict, before: str, after: str) -> dict:
    """对 funnel trace 的 results 做 P0-4 时间窗口客户端过滤。

    funnel 返回结构为 {status, trace, results, ...}；这里复用 engine
    的时间归一化逻辑，失败则原样返回（降级不阻断检索）。
    """
    try:
        results = result.get("results") or []
        if not isinstance(results, list):
            return result
        _filter_results_by_time(results, before, after)
        result["results"] = results
        if "trace" in result and isinstance(result["trace"], dict):
            result["trace"]["final_count"] = len(results)
        return result
    except Exception:
        return result


def _filter_results_by_time(results: list, before: str, after: str) -> None:
    """原地过滤 results，剔除不在 before/after 窗口内的候选（P0-4）。

    与 engine.RecallEngine.search 的窗口过滤同一套四级时间戳回退语义。
    """
    try:
        from ducky.engine import extract_timestamp

        if not before and not after:
            return
        b_prefix = None
        a_prefix = None
        try:
            from ducky.engine import _parse_time_boundary
            b_prefix = _parse_time_boundary(before)
            a_prefix = _parse_time_boundary(after)
        except Exception:
            from ducky.engine import _date_prefix
            b_prefix = _date_prefix(before)
            a_prefix = _date_prefix(after)

        kept = []
        for item in results:
            if not isinstance(item, dict):
                continue
            ts = extract_timestamp(item)
            prefix = ""
            if ts > 0:
                from datetime import datetime, timezone
                prefix = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if not prefix:
                kept.append(item)  # 无时间戳保守保留
                continue
            if b_prefix and prefix > b_prefix:
                continue
            if a_prefix and prefix < a_prefix:
                continue
            kept.append(item)
        results[:] = kept
    except Exception:
        return


# ── 召回强度标注（v20 P0-6 同案）────────────────────────────────────────────
#
# 生产实测的分数分布（生产实例，8 个探针）：
#     真问题  top = 0.558 / 0.560 / 0.662 / 0.651
#     纯噪声  top = 0.457 / 0.423 / 0.466
# 两组不重叠，也就是说**嵌入是有区分力的**。（第一次测时我拿「zzz9x9x9x 不存在的
# 话题」当噪声，它拿到 0.589 —— 但那句里带着「不存在的话题」四个真词，压根不是噪声。
# 一个选坏了的探针差点让我得出「分数毫无区分力」的相反结论。）
#
# 真实缺陷是：**纯噪声照样返回满额结果，且没有任何「这批很弱」的信号**。
# 用户拿到 5 条分数 0.40–0.47 的东西，和拿到 5 条 0.66 的东西，在响应里长得一模一样。
#
# 为什么只标注、不默认过滤：8 个数据点定不出一个生产阈值 —— 那正是「拍脑袋常数」。
# 默认过滤一旦把阈值定高，丢掉的是真记忆，比多返回几条噪声严重得多。所以：
#   · 默认（floor=0）：只标注，不丢任何结果，行为与整改前逐字节一致；
#   · 部署方显式设 `AIDUMEM_RECALL_SCORE_FLOOR` 才启用过滤，且过滤掉几条要报出来。
_SCORE_FLOOR_ENV = "AIDUMEM_RECALL_SCORE_FLOOR"


def _score_floor() -> float:
    """读取召回下限；显式设了就用它，**没设则回落到已标定的置信下限**。

    🔴 社区 Issue #5（2026-08-29）：这里原本「未设 = 0.0 = 只标注不过滤」，
    于是出现了一个自相矛盾的局面 —— 部署方明明已经用
    ``AIDUMEI_RECALL_VERDICT_THRESHOLD`` 声明了「低于这个分就不可信」
    （生产实测配的是 0.46，那是拿真实查询分布标定出来的），
    系统也照此把整批结果判成 ``not_found``，**却仍然把它们原样返回**。
    「我知道这批不靠谱」和「我照样给你」同时成立。

    实机实测（2026-08-29，生产库三条样本）：

      查询「复盘召回质量」 → 真相关 0.7165 · 无关 0.4062 · 无关 0.3870
      查询「量子色动力学的渐近自由」 → 三条全无关 0.2862 / 0.2819 / 0.2362

    以 0.46 为下限，两个查询同时得到正确结果（前者只留真相关那条，
    后者空手）。**这个数不是拍脑袋的**，它是部署方已经标定并在用的那一个。

    **回落而不是新造一个默认值**，理由是单一真相源：一个部署对「多少分算可信」
    只该有一个说法。要关掉过滤，显式写 ``AIDUMEM_RECALL_SCORE_FLOOR=0``。

    值非法时打 warning 而不是静默当 0 —— 「设了一个打错的阈值」和「没设」
    在行为上一样，但在意图上完全不同（铁律 13）。
    """
    raw = (os.environ.get(_SCORE_FLOOR_ENV) or "").strip()
    if not raw:
        return _verdict_threshold()
    try:
        v = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，本次按不过滤处理（只标注）", _SCORE_FLOOR_ENV, raw[:20])
        return 0.0
    if not (0.0 <= v <= 1.0):
        logger.warning("%s=%s 超出 [0,1]，本次按不过滤处理（只标注）", _SCORE_FLOOR_ENV, v)
        return 0.0
    return v


def annotate_recall_strength(results: list, floor: float | None = None) -> dict:
    """给召回结果打强度标注，返回随响应下发的元信息。

    返回 `{"top_score", "floor", "weak", "dropped"}`：
      · `top_score` —— 本次最高分（无结果时 None，**不是 0.0**：0.0 会被读成
        「有结果但都是 0 分」，那是另一件事）
      · `weak`      —— 最高分低于 floor（floor=0 时恒 False）
      · `dropped`   —— 被 floor 过滤掉的条数（floor=0 时恒 0）
    """
    f = _score_floor() if floor is None else floor
    scores = [r.get("score") for r in results if isinstance(r, dict)]
    nums = [float(x) for x in scores if isinstance(x, (int, float))]
    top = max(nums) if nums else None
    dropped = 0
    if f > 0.0 and results:
        keep = [r for r in results
                if not isinstance(r, dict)
                or not isinstance(r.get("score"), (int, float))
                or float(r["score"]) >= f]
        dropped = len(results) - len(keep)
        results[:] = keep
    return {
        "top_score": round(top, 4) if top is not None else None,
        "floor": f,
        "weak": bool(top is not None and f > 0.0 and top < f),
        "dropped": dropped,
    }


# ── 召回弃答信号（v20.1 WP-C）─────────────────────────────────────────
# 「5 条 0.66」和「5 条 0.12」在响应里长得一样是 v20 修过的（_recall_strength），
# 但「空结果」仍然是一个词说两件事：库里确实没有 vs 嵌入服务挂了召回空转。
# recall_verdict 把它拆成三态，且**故障先于缺失** —— 组件坏了绝不冒充「查无此忆」。
_VERDICT_THRESHOLD_ENV = "AIDUMEI_RECALL_VERDICT_THRESHOLD"


def _verdict_threshold() -> float:
    """not_found 的置信下限；未设置或非法一律 0.0（= 只有空结果才判 not_found）。

    默认 0.0 不是偷懒，是纪律：本机没有足够查询分布去定一个生产阈值，
    拍脑袋常数会把真记忆判成「没有」。校准值属于部署配置决策，用生产侧
    沙箱的真实查询分布算分位数后再设。值非法时打 warning 不静默（铁律 13：
    「设了一个打错的阈值」和「没设」行为一样、意图完全不同）。
    """
    raw = (os.environ.get(_VERDICT_THRESHOLD_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        v = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，本次按 0.0 处理（只有空结果才判 not_found）",
                       _VERDICT_THRESHOLD_ENV, raw[:20])
        return 0.0
    if not (0.0 <= v <= 1.0):
        logger.warning("%s=%s 超出 [0,1]，本次按 0.0 处理", _VERDICT_THRESHOLD_ENV, v)
        return 0.0
    return v


def compute_recall_verdict(
    results: list,
    top_score: float | None,
    threshold: float,
    *,
    vector_leg_failed: bool = False,
    recall_path: str = "hybrid",
) -> tuple[str, str]:
    """三态判定：(verdict, basis)。判定顺序就是契约 —— degraded 先于 not_found。

    · degraded  —— 空结果由故障产生（向量腿断 / 混合召回整体降级后仍空）。
                   这时「没有」是不可知，不是不存在。
    · not_found —— 真空结果，或最高分低于显式配置的置信下限
                   （结果照常返回，verdict 只是随行判语，不越权丢数据）。
    · found     —— 有结果且过线。
    """
    if not results:
        if vector_leg_failed or recall_path == "mem0_degraded":
            return "degraded", "empty_after_leg_failure"
        return "not_found", "empty_results"
    if threshold > 0.0 and (top_score is None or top_score < threshold):
        return "not_found", "below_threshold"
    return "found", "scored"


def register_search_routes(app: FastAPI) -> None:
    @app.post("/search", response_model=SearchResponse)
    def search(req: SearchRequest):
        """搜索记忆 — Workspace 优先 → 混合召回（Hybrid）→ Salience boost"""
        # v20.2.5（用户实测 Y-NEW1）：空 query 不再返回随机记忆。
        #
        # 实测 `POST /search {"query":""}` → HTTP 200 + 1 条 score 0.496 的结果。
        # 用户搜了个空却拿到一条记忆，第一反应是「我搜了个寂寞？」，第二反应是
        # 怀疑召回是不是一直在瞎给。gate 端点早有 empty_query 处理，主路由没有。
        #
        # 用**判语**而不是 422：空搜索不是客户端错误，它是一次「没有可查的东西」
        # —— 与既有三态（found / not_found / degraded）同一个体系，调用方
        # 一处判断全覆盖，不必为它单开一条异常分支。
        if not (req.query or "").strip():
            return {
                "status": "ok",
                "results": [],
                "recall_verdict": "empty_query",
                "recall_confidence": 0.0,
                "verdict_basis": "query 为空，未执行召回",
            }
        try:
            # 注意：/search 是显式搜索 API，不走 relevance gate（gate 用于对话上下文注入）
            # v20 P0-4：每请求重置 rerank 遥测——线程复用时上一请求的残留
            # 会被误读成本次的重排序结局。
            from ducky.mem0_runtime import last_rerank_telemetry, reset_rerank_telemetry
            reset_rerank_telemetry()
            # v20.1 WP-C：召回腿遥测与 rerank 同规矩——每请求重置，
            # 线程复用时上一请求的腿断残留不许被读成本次的。
            from ducky.engine import last_recall_telemetry, reset_recall_telemetry
            reset_recall_telemetry()
            recall_path = "hybrid"
            mem = get_memory()
            scope = make_scope(req.user_id, req.bank_id)
            uid = _normalize_user_id(scope.user_id)
            bank_id = scope.bank_id
            ensure_bank_registered(make_scope(uid, bank_id))

            try:
                from ducky.memory_workspace import ws_lookup, ws_feed_from_results
                ws_hits = ws_lookup(uid, req.query, bank_id=bank_id)
                if ws_hits:
                    boost_salience_for_results(ws_hits)
                    # v20.1 整改轮（R-06 · 外审 z P2-04）：本分支的三态字段集
                    # 必须与 hybrid 分支一致 —— 上层按「有 confidence 才信」
                    # 决策时，缺字段的 found 会被漏判或误判。
                    ws_strength = annotate_recall_strength(ws_hits)
                    return {
                        "status": "ok", "results": ws_hits,
                        "_workspace_hit": True,
                        "_recall_path": "workspace",
                        "_rerank": {"status": "not_invoked"},
                        "_recall_strength": ws_strength,
                        "_recall_legs": {"workspace": "hit"},
                        # workspace 命中 = 热缓存里真有 —— found，无歧义。
                        "recall_verdict": "found",
                        "verdict_basis": "workspace_hit",
                        "engine_mode": __import__("ducky.gear", fromlist=["current_mode"]).current_mode(),
                        "recall_confidence": ws_strength["top_score"],
                    }
            except ImportError:
                pass

            results = []
            effective_limit = req.top_k if req.top_k and req.top_k > 0 else req.limit
            try:
                results = lazy_import_hybrid()(
                    mem, req.query, uid, effective_limit,
                    before=req.before, after=req.after,
                    bank_id=bank_id,
                )
                logger.info(f"🔍 hybrid 召回: query='{req.query}' user_id='{_normalize_user_id(req.user_id)}' → {len(results)} 条")
            except Exception as e:
                recall_path = "mem0_degraded"
                logger.debug(f"混合召回不可用，降级 mem0 搜索: {e}")
                raw = mem.search(req.query, filters=vector_scope_filters(uid, bank_id), top_k=max(effective_limit * 3, 20))
                results = raw.get("results", raw) if isinstance(raw, dict) else raw
                # 🔴v20：默认域不下推 bank_id（下推=清空存量），命名域的点在这里剔除。
                results = [it for it in (results or []) if vector_item_in_bank(it, bank_id)]
                if req.before or req.after:
                    # 降级路径也必须兑现 P0-4 时间窗口，否则混合召回一挂
                    # before/after 就被静默丢弃，时间推理返回错误结果。
                    _filter_results_by_time(results, req.before, req.after)
                logger.info(f"🔍 mem0 裸搜: query='{req.query}' user_id='{_normalize_user_id(req.user_id)}' → {len(results)} 条")

            boost_salience_for_results(results)
            _annotate_memory_types(results, user_id=_normalize_user_id(req.user_id),
                                   bank_id=getattr(req, "bank_id", "default") or "default")

            # 📼 v19.4.0 明镜工程 Phase 1: Verbatim Vault 原文证据融合
            # 在既有召回结果之上，并行检索原文层并融合返回（主干优先、保留配额、
            # 失败干净降级）。让召回的不只是蒸馏后的事实，还有说过的原话。
            try:
                from ducky.verbatim_vault import verbatim_search, fuse_verbatim
                v_hits = verbatim_search(req.query, uid, limit=effective_limit, bank_id=bank_id)
                if v_hits:
                    results = fuse_verbatim(results, v_hits, limit=effective_limit, query=req.query)
            except Exception as _ve:
                logger.debug(f"📼 [VerbatimVault] 原文融合跳过: {_ve}")

            try:
                from ducky.memory_workspace import ws_feed_from_results
                ws_feed_from_results(uid, results, bank_id=bank_id)
            except ImportError:
                pass

            # v20 P0-4：召回路径与 rerank 三态随响应返回——「降级裸搜」和
            # 「重排序其实没生效」此前只活在服务端日志里，调用方无从察觉。
            rerank_telem = last_rerank_telemetry() or {"status": "not_invoked"}
            # v20：召回强度随响应下发。整改前「5 条 0.66」和「5 条 0.42」
            # 在响应里长得一模一样，调用方无从判断这批东西值不值得信。
            strength = annotate_recall_strength(results)
            if strength["dropped"]:
                logger.info("召回下限过滤掉 %d 条（floor=%s, top=%s）",
                            strength["dropped"], strength["floor"], strength["top_score"])
            # v20.1 WP-C：三态判语随响应下发。上层 Agent 据此决定「引用记忆 /
            # 承认没有 / 报告记忆系统故障」—— 三种回应，不该由一个空列表通吃。
            recall_telem = last_recall_telemetry()
            verdict, verdict_basis = compute_recall_verdict(
                results,
                strength["top_score"],
                _verdict_threshold(),
                # v20.2：云腿断但本请求已落本地腿——备胎拿出货就是 found
                # （engine_mode=lite 如实标注）；备胎空手 = 系统能力受损，
                # 仍判 degraded，绝不冒充「查无此忆」。
                vector_leg_failed=(
                    recall_telem.get("vector_leg") == "failed"
                    or (recall_telem.get("vector_leg") == "local_fallback"
                        and not results)
                ),
                recall_path=recall_path,
            )
            if verdict == "degraded":
                logger.warning("召回判语 degraded：%s（vector_leg=%s）",
                               verdict_basis, recall_telem.get("error", "-"))
            # v20.2 自动挡（WP-H）：挡位如实随响应下发。lite 挡的置信分
            # 是本地模型口径，与云模型分数不可直接比较 —— 降挡是保命，
            # 不是无损平替，口径差异必须让调用方看得见。
            from ducky.gear import gear_status
            _gs = gear_status()
            # 本次请求实际用的腿比系统挡位更诚实：half-open 探测成功的那次
            # 查询真真切切吃的是云腿，报 lite 反而是撒谎。
            _leg = (recall_telem or {}).get("vector_leg", "")
            # v20.2.3（外审 A-4 配套）：先看**部署配置**再看腿。本地档下云腿
            # 被整条关闭，gear_status 会如实报 disabled_by_policy —— 那是给
            # 运维看的探针词，不该外泄进 /search 的响应契约（调用方按
            # full|lite 解析）。本地档恒在本地腿上，就报 lite。
            from ducky.engine_mode import cloud_leg_enabled
            if not cloud_leg_enabled():
                _this_mode = "lite"
            elif _leg in ("local", "local_fallback"):
                _this_mode = "lite"
            else:
                _this_mode = _gs["mode"]
            resp = {
                "status": "ok", "results": results,
                "_recall_path": recall_path,
                "_rerank": rerank_telem,
                "_recall_strength": strength,
                "_recall_legs": recall_telem,
                "recall_verdict": verdict,
                "verdict_basis": verdict_basis,
                "recall_confidence": strength["top_score"],
                "engine_mode": _this_mode,
            }
            if _this_mode == "lite":
                resp["engine_mode_reason"] = _gs.get("last_shift_reason")
                resp["confidence_scale"] = "local-bge-small-zh"
            return resp
        except Exception as e:
            logger.error(f"search 失败: {e}")
            return {"status": "error", "results": [], "detail": str(e)}

    @app.post("/search_trace")
    def search_trace(req: SearchRequest):
        """搜索记忆 + Recall Funnel trace（带分阶段耗时）"""
        try:
            mem = get_memory()
            effective_limit = req.top_k if req.top_k and req.top_k > 0 else req.limit
            scope = make_scope(req.user_id, req.bank_id)
            result = lazy_import_funnel()(mem, req.query, _normalize_user_id(scope.user_id), effective_limit, bank_id=scope.bank_id)
            # P0-4：与 /search 保持一致的时间窗口过滤。funnel 若返回
            # results 列表，这里做一次客户端过滤，不改变 trace 结构。
            if req.before or req.after:
                result = _apply_time_window_to_trace(result, req.before, req.after)
            return result
        except ImportError:
            raise HTTPException(503, "Recall Funnel 模块未就绪")
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"search_trace 失败: {e}")
            return {
                "status": "error",
                "trace": {"stages": [], "total_ms": 0, "final_count": 0},
                "results": [],
                "detail": str(e),
            }

    # 🔴1：Tahoe-Gate 相关性闸门端点。此前 relevance_check 全库零生产调用，
    # 「省 Token」卖点是画饼。现暴露为 /gate，供宿主 Agent 在对话上下文注入前
    # 先问一句「这轮要不要检索记忆」——闲聊直接跳过检索，真正省掉 Token 与算力。
    @app.get("/gate")
    def gate(query: str = "", text: str = "", q: str = ""):
        """相关性闸门：判断这条 query 是否需要检索记忆上下文。

        兼容 query / text / q 三种入参键名。
        返回 {needs_memory, reason, scope}。宿主在注入记忆前调用它，
        needs_memory=false 时可整轮跳过 /search，省掉无谓的向量检索。
        """
        actual_query = (query or text or q or "").strip()
        if not actual_query:
            return {"status": "ok", "needs_memory": False, "reason": "empty_query", "scope": None}
        try:
            from ducky.pipeline.memory_gate import relevance_check
            return {"status": "ok", **relevance_check(actual_query)}
        except Exception as e:
            logger.error(f"gate 失败: {e}")
            return {"status": "error", "needs_memory": True, "reason": f"gate_error: {e}", "scope": None}

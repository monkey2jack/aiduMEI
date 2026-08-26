"""ducky.hot.health — GET /health & /metrics（v19.2.0 可观测性升级版）"""
from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI

from ducky.version import SERVICE_VERSION, CODENAME, CODENAME_ZH

# 版本信息：默认绑定 ducky.version，亦支持由 api_server.py 启动时通过 set_version_info() 注入
_version_info = {
    "service_version": SERVICE_VERSION,
    "codename": CODENAME,
    "codename_zh": CODENAME_ZH,
}


def set_version_info(version: str, codename: str | None = None, codename_zh: str | None = None):
    """api_server 启动时调用，注入版本信息到 health 端点"""
    _version_info["service_version"] = version
    _version_info["codename"] = codename
    _version_info["codename_zh"] = codename_zh


from ducky.mem0_runtime import (
    is_mem_ready,
    lazy_import_funnel,
    lazy_import_hybrid,
    lazy_import_layer1,
)
from ducky.tool_envelope import ok as te_ok
from ducky.utils import FACTS_DB, TEXT_FTS_DB
from ducky.degradation import DegradationTracker

logger = logging.getLogger("aiduMEM.hot")


def register_health_routes(app: FastAPI) -> None:
    @app.get("/health")
    def health():
        """B 档：lazy 预热 + 真实探针 + 反静默降级追踪 + 水位预警。"""
        module_ok = {}
        try:
            lazy_import_layer1()
            module_ok["layer1_selfcheck"] = True
        except Exception as e:
            module_ok["layer1_selfcheck"] = False
            logger.debug(f"health layer1: {e}")
        try:
            lazy_import_funnel()
            module_ok["recall_funnel"] = True
        except Exception as e:
            module_ok["recall_funnel"] = False
            logger.debug(f"health funnel: {e}")
        try:
            lazy_import_hybrid()
            module_ok["hybrid_recall"] = True
        except Exception as e:
            module_ok["hybrid_recall"] = False
            logger.debug(f"health hybrid: {e}")

        def _can_import(mod: str) -> bool:
            try:
                __import__(mod)
                return True
            except Exception:
                return False

        module_ok.update({
            "v8_ignition":    _can_import("ducky.pipeline.memory_ignition"),
            "v8_workspace":   _can_import("ducky.pipeline.memory_workspace"),
            "v8_broadcast":   _can_import("ducky.federation.broadcast"),
            "v8_jlens":       _can_import("ducky.pipeline.memory_jlens"),
            "v8_persistence": _can_import("ducky.pipeline.memory_persistence"),
            "v2.1_salience":  _can_import("ducky.salience.core"),
            "v2.1_gate":      _can_import("ducky.pipeline.memory_gate"),
            "v2.1_envelope":  _can_import("ducky.tool_envelope"),
            "v18.3_obsidian": _can_import("ducky.routes_obsidian"),
            "scoring_engine": _can_import("ducky.scoring"),
            "wal_engine":     _can_import("ducky.wal_engine"),
            "injection_guard": _can_import("ducky.security.injection_guard"),
            "v20.1_pattern_extract": _can_import("ducky.pattern_extract"),
        })

        probes: dict[str, object] = {
            "facts_db": os.path.exists(FACTS_DB),
            "text_fts_db": os.path.exists(TEXT_FTS_DB),
            "mem0_singleton": is_mem_ready(),
            "port_service": True,
            "injection_guard_ok": True,
        }

        # v20 scope/backend contract: expose the actual default bank and
        # vector backend capability without making health depend on optional
        # sqlite-vec or a running Qdrant server.  A failed optional probe is a
        # named degradation, never an unexplained empty search result.
        try:
            from ducky.bank_contract import DEFAULT_BANK_ID, ensure_memory_banks_schema
            from ducky.utils import get_facts_conn
            _bank_conn = get_facts_conn()
            try:
                _bank_state = ensure_memory_banks_schema(_bank_conn)
            finally:
                # get_facts_conn 返回线程复用代理，close() 是 no-op；
                # 这里保持与本文件其余探针一致的 close 习惯即可，无泄漏。
                _bank_conn.close()
            probes["default_bank_id"] = DEFAULT_BANK_ID
            probes["memory_banks_ok"] = _bank_state.get("status") == "ok"
            if probes["memory_banks_ok"] is False:
                probes["memory_banks_error"] = _bank_state.get("detail", "schema migration failed")
        except Exception as _bank_exc:
            probes["memory_banks_ok"] = False
            probes["memory_banks_error"] = str(_bank_exc)[:120]

        # v20.1 WP-A：确定性抽取层 —— 生效开关问判定函数本身（不问配置
        # 文件），计数随附。开关值非法时错误原文进探针：那是「显式配置
        # 无效」的报警，不许被压成一个安静的 False。
        try:
            from ducky.pattern_extract import is_pattern_extract_enabled
            from ducky.pattern_extract import stats as _pattern_stats
            probes["pattern_extract"] = {
                "enabled": is_pattern_extract_enabled(), **_pattern_stats(),
            }
        except Exception as _pe_exc:
            probes["pattern_extract"] = {
                "enabled": False, "error": str(_pe_exc)[:120],
            }

        # v20.1 WP-C：弃答判语的置信下限 —— 生效值问判定函数（它含非法值
        # 回退逻辑），不问环境变量原文。0.0 = 只有空结果才判 not_found。
        try:
            from ducky.hot.search import _verdict_threshold
            _vt = _verdict_threshold()
            probes["recall_verdict_threshold_effective"] = _vt
            # v20.1 整改轮（R-13 · 外审 y 变体 + w P1-④）：阈值 0.0 是合法
            # 默认（只有空结果才判 not_found），但生产侧应基于真实查询
            # 分布校准。提示走 probe 字段而非 warnings —— 常驻 warning
            # 会制造新的告警疲劳，这是提示不是告警。
            if _vt == 0.0:
                probes["recall_verdict_threshold_hint"] = (
                    "未配置显式置信下限：当前仅空结果判 not_found。"
                    "生产建议按真实查询分布分位数校准 AIDUMEI_RECALL_VERDICT_THRESHOLD"
                )
        except Exception as _vt_exc:
            probes["recall_verdict_threshold_error"] = str(_vt_exc)[:120]

        # v20.2 自动挡（WP-H）：挡位、熔断器内态、备胎在场性、欠账水位、
        # 本地索引点数——「现在跑在哪个挡上」运维面一眼可见。
        try:
            from ducky.gear import gear_status
            probes["engine_gear"] = gear_status()
        except Exception as _gexc:
            probes["engine_gear"] = {"error": str(_gexc)[:120]}
        try:
            from ducky.local_embed import local_embed_status
            probes["local_embed"] = local_embed_status()
        except Exception as _lexc:
            probes["local_embed"] = {"error": str(_lexc)[:120]}
        try:
            from ducky.dual_index import local_point_count, pending_counts
            probes["local_index_points"] = local_point_count()
            probes["pending_embeddings"] = pending_counts()
        except Exception as _dexc:
            probes["dual_index_error"] = str(_dexc)[:120]

        # v20.1.1（N-1）：限流生效值可查——配置生效三查的运维面。非法
        # env 的报错原文进探针，与阈值/开关探针同一纪律：不静默。
        try:
            from ducky.rate_guard import add_rate_limit, delete_all_rate_limit
            probes["rate_add_per_min_effective"] = add_rate_limit()
            probes["rate_delete_all_per_min_effective"] = delete_all_rate_limit()
        except ValueError as _rl_exc:
            probes["rate_limit_config_error"] = str(_rl_exc)[:160]

        # v20.1 WP-D1：核心记忆向量索引开关生效值。值非法时错误原文进探针
        # —— 那是「显式配置无效」的报警，不许压成一个安静的 False。
        try:
            from ducky.core_memory import is_core_vector_index_enabled
            probes["core_vector_index_enabled"] = is_core_vector_index_enabled()
        except Exception as _cv_exc:
            probes["core_vector_index_enabled"] = False
            probes["core_vector_index_error"] = str(_cv_exc)[:120]

        # v20 回归清单：schema_version 必须可读。
        # 只报代码里的常量是**假绿灯**——库还停在 v1 时它照样报 2。
        # 所以 schema_version 取磁盘上的 PRAGMA user_version（真相），
        # 代码期望值另开一个字段，两者不一致就记名降级。
        try:
            from ducky.schema_bootstrap import CURRENT_SCHEMA_VERSION
            from ducky.utils import get_facts_conn
            _sv_conn = get_facts_conn()
            try:
                _on_disk = int(_sv_conn.execute("PRAGMA user_version").fetchone()[0])
            finally:
                _sv_conn.close()
            probes["schema_version"] = _on_disk
            probes["schema_version_expected"] = int(CURRENT_SCHEMA_VERSION)
            probes["schema_version_ok"] = _on_disk == int(CURRENT_SCHEMA_VERSION)
            if not probes["schema_version_ok"]:
                # 用模块级的 DegradationTracker（本文件顶部已 import）。
                # 这里若再写一次函数内 import，Python 会把这个名字变成**整个函数的局部变量**，
                # 于是版本对得上（不进本分支）时，函数末尾那处引用直接 UnboundLocalError
                # —— /health 在「一切正常」的情况下 500。护栏的 bug 会伪装成产品的 bug。
                DegradationTracker.record_degradation(
                    "schema_version", f"on-disk user_version={_on_disk} != expected {CURRENT_SCHEMA_VERSION}"
                )
        except Exception as _sv_exc:
            probes["schema_version"] = None
            probes["schema_version_ok"] = False
            probes["schema_version_error"] = str(_sv_exc)[:120]

        try:
            from ducky.vector_backend import backend_health
            _backend = backend_health()
            probes["vector_backend"] = _backend.get("backend")
            probes["vector_backend_ok"] = bool(_backend.get("ok"))
            probes["vector_backend_degraded"] = list(_backend.get("degraded") or [])
            # ok=False 有两种：真故障，和「单例还没起、根本没探」。
            # 必须让运维一眼分得开，否则冷启动的常态会被当成事故，
            # 几次假警报之后这一栏就没人看了。
            probes["vector_backend_probed"] = bool(_backend.get("probed"))
            if _backend.get("error"):
                probes["vector_backend_error"] = str(_backend["error"])[:120]
            elif _backend.get("detail"):
                probes["vector_backend_detail"] = str(_backend["detail"])[:120]
        except Exception as _backend_exc:
            probes["vector_backend"] = "qdrant"
            probes["vector_backend_ok"] = False
            probes["vector_backend_error"] = str(_backend_exc)[:120]

        # rerank 配置探针（v20 P0-4）：只报「配没配、配的谁」，不做真实外呼
        # （health 不该烧付费 API），调用期三态（ok/error/empty）在 /search
        # 响应的 _rerank 字段与 /usage 账本里。
        try:
            from ducky.mem0_runtime import rerank_config_status
            _rr = rerank_config_status()
            probes["rerank_configured"] = bool(_rr.get("configured"))
            if _rr.get("configured"):
                probes["rerank_provider"] = _rr.get("provider")
        except Exception as e:
            probes["rerank_configured"] = False
            probes["rerank_error"] = str(e)[:120]

        # WAL 探针
        try:
            from ducky.wal_engine import WALEngine
            wal = WALEngine.get_instance()
            pending_count = len(wal.get_pending_entries())
            probes["wal_engine_ok"] = True
            probes["wal_pending_entries"] = pending_count
        except Exception as e:
            probes["wal_engine_ok"] = False
            probes["wal_error"] = str(e)[:120]

        # FTS 探针
        try:
            from ducky.utils import get_text_conn
            conn = get_text_conn()
            n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            conn.close()
            probes["fts_memories"] = int(n)
            probes["fts_ok"] = True
        except Exception as e:
            probes["fts_ok"] = False
            probes["fts_error"] = str(e)[:120]

        # 实体词表探针
        warnings: list[str] = []
        try:
            from ducky.pipeline.memory_gate import entity_keywords_status
            ek = entity_keywords_status()
            probes["entity_keywords"] = ek["count"]
            probes["entity_keywords_ok"] = ek["configured"]
            # v20 P0-3：来源也要暴露。「22 个词」这个数字本身不能证明配置是对的 ——
            # 它可能来自一个我们以为已经删掉的 systemd drop-in（那份 drop-in 会静默
            # 压过 .env，见 memory_gate.entity_keywords_source 的注释）。
            probes["entity_keywords_source"] = ek.get("source", "unknown")
            if not ek["configured"]:
                warnings.append(
                    f"{ek['env_var']} 未配置：涉及自定义人名/项目代号的查询会零召回，"
                    "参考 .env.example 配置后重启服务"
                )
            elif ek.get("source") == "overridden":
                warnings.append(
                    f"{ek['env_var']} 生效值与 .env 声明值不一致：有东西在覆盖唯一真相源"
                    "（多半是 systemd drop-in 的 Environment= 排在 EnvironmentFile= 之后）。"
                    "改 .env 不会生效，配置与现实是两半"
                )
            elif ek.get("source") == "outside_env_file":
                warnings.append(
                    f"{ek['env_var']} 有生效值，但 .env 里没有这一行：来源不在唯一真相源里。"
                    "下一次照 .env 重建部署时它会静默消失，而 /health 在那之前一直是绿的"
                )
            elif ek.get("source") == "declared_not_effective":
                warnings.append(
                    f"{ek['env_var']} 在 .env 里声明了但没有生效：配置写了不等于配置生效"
                )
        except Exception as e:
            probes["entity_keywords_ok"] = False
            probes["entity_keywords_error"] = str(e)[:120]

        # HTTP 结局探针（v20 P1-9）
        #
        # 整改前 `/health` 只探活：服务 active、探针全 ok，而三分之一的请求在报 500 ——
        # 「195 次 500 / 13 分钟」那次事故就是这个形态，事后没有任何可复现的监控路径。
        try:
            from ducky import http_metrics
            hm = http_metrics.snapshot()
            probes["http_error_rate_5m"] = hm["error_rate_5m"]
            probes["http_requests_5m"] = hm["total"]
            probes["http_server_errors_5m"] = hm["server_errors"]
            probes["http_client_errors_5m"] = hm["client_errors"]
            rate = hm["error_rate_5m"]
            if rate is not None and rate > 0:
                warnings.append(
                    f"近 {hm['window_s']}s 内 5xx 占比 {rate:.1%}"
                    f"（{hm['server_errors']}/{hm['total']}）：服务在出错，而探活是绿的"
                )
        except Exception as e:
            probes["http_error_rate_5m"] = None
            probes["http_metrics_error"] = str(e)[:120]

        # WAL 水位（v20）：生产实测三个库的 WAL 都胀到 4MB 长期未 checkpoint。
        # 不影响正确性，但崩溃恢复时间随它线性增长，而且主库 mtime 会因此骗人。
        try:
            from ducky.wal_watermark import snapshot as _wal_snap
            w = _wal_snap()
            probes["wal_total_bytes"] = w["total_wal_bytes"]
            probes["wal_alert_dbs"] = w["alerts"]
            if w["alerts"]:
                warnings.append(
                    "以下库的 WAL 已超过主库体积、长期未 checkpoint：%s —— "
                    "崩溃恢复时间随它增长，且主库 mtime 会因此失真"
                    % "、".join(w["alerts"])
                )
        except Exception as e:
            probes["wal_total_bytes"] = None
            probes["wal_watermark_error"] = str(e)[:120]

        # 进程资源占用（v20：部署方指定为产品级指标）
        #
        # 一个记忆引擎如果内存单调上涨、fd 只增不减、线程越跑越多，那不是「性能差」，
        # 是迟早会把用户的记忆一起带走。所以它和召回准确度是同一级别的指标，
        # 该常驻在 /health 上，而不是等出事了再上机器手测一次。
        # 字段语义严格区分「当前 rss」与「历史峰值 max_rss」—— 混成一个字段会让
        # 一次早已结束的尖峰永远挂在监控上（详见 ducky/resource_probe.py）。
        try:
            from ducky.resource_probe import snapshot as _res_snapshot
            res = _res_snapshot()
            probes["process_rss_mb"] = res["rss_mb"]
            probes["process_max_rss_mb"] = res["max_rss_mb"]
            probes["process_cpu_seconds"] = res["cpu_seconds"]
            probes["process_threads"] = res["threads"]
            probes["process_open_fds"] = res["open_fds"]
        except Exception as e:
            probes["process_rss_mb"] = None
            probes["process_resource_error"] = str(e)[:120]

        # 特性级失败计数（v20 P1-8）
        #
        # AST 普查实测：ducky/ + 三个服务入口共 489 处宽捕获，其中 251 处在生产
        # 默认日志级别下**等于无声**。全改会用噪声淹掉真信号，所以只改「挂在写入／
        # 读取主链路上、失败后有持久用户可见后果」的那些：索引没建（搜不到）、
        # 原文没存（永久丢失）、自编辑没跑（去重从未执行）、重排没跑（排序降级）。
        # 每一处都要能回答铁律 8 那句「如果这里真失败了，谁会知道？」——
        # 下面这两行就是那个「谁」。
        try:
            from ducky.failure_ledger import snapshot as _fl_snapshot
            fl = _fl_snapshot()
            probes["feature_failures"] = fl["total"]
            probes["feature_failures_by_name"] = fl["by_feature"]
            if fl["total"]:
                top = sorted(fl["by_feature"].items(), key=lambda kv: -kv[1])[:3]
                warnings.append(
                    "本进程有特性级失败 %d 次（%s）：主链路都降级继续了，"
                    "但这些事情没有发生 —— 记忆可能搜不到、原文可能没存下"
                    % (fl["total"], "，".join(f"{k}×{v}" for k, v in top))
                )
        except Exception as e:
            probes["feature_failures"] = None
            probes["feature_failures_error"] = str(e)[:120]

        # 核心记忆陈旧度探针（v20 P0-4）
        #
        # 为什么这条必须在运维面上：`inject_context()` 早就会给超期 block 打 ⚠️，
        # 但那条信息只出现在**注入给模型的上下文里**。也就是说「核心记忆一个月没更新」
        # 这件事，此前只有人正好去读一次注入内容才会发现 —— 没有任何自动化手段。
        # 而它的症状偏偏是最难自己暴露的那种：东西在，只是旧的，答案语气照样自信。
        try:
            from ducky.core_memory import staleness_status
            cm = staleness_status()
            probes["core_memory_blocks"] = cm["blocks"]
            probes["core_memory_stale"] = cm["stale"]
            probes["core_memory_stale_blocks"] = cm["stale_blocks"]
            probes["core_memory_oldest_age_days"] = cm["oldest_age_days"]
            probes["core_memory_unfilled_blocks"] = cm["unfilled_blocks"]
            # v20.1 WP-D2：分级阈值的生效值上探针 —— 验收问这里，不问配置文件。
            probes["core_memory_thresholds"] = cm.get("threshold_days_by_block", {})
            # v20.1 整改轮（R-16 · 外审 w P1-② 机制 + y 覆盖度建议）：
            # 三副本对账。三腿写入都是软失败设计，没有对账，缺腿的块
            # 从此静默检索不到而绿灯依旧。只观测不自愈；占位块与影子行
            # 已在 audit 内排除（把设计行为算成缺腿会制造新的告警疲劳）。
            try:
                from ducky.core_memory import audit_core_replicas
                _rep = audit_core_replicas()
                probes["core_replica_checked"] = _rep["checked"]
                probes["core_replica_gaps"] = _rep["gaps"]
                probes["core_replica_vector_checked"] = _rep["vector_checked"]
                if any(g.get("vector") is False for g in _rep["gaps"]):
                    probes["core_replica_hint"] = (
                        "存量核心记忆未入向量池：获批后运行 "
                        "scripts/backfill_core_vectors.py（dry-run 先行）"
                    )
            except Exception as _rep_exc:
                probes["core_replica_error"] = str(_rep_exc)[:120]
            if cm["stale"]:
                warnings.append(
                    f"核心记忆有 {cm['stale_blocks']}/{cm['blocks']} 块超过各自"
                    f"陈旧阈值未更新（最旧 {cm['oldest_age_days']} 天，分级阈值见 "
                    "core_memory_thresholds 探针）："
                    "问「现在在做什么」会拿到过期答案，且语气与新鲜答案毫无区别"
                )
            if cm["unfilled_blocks"]:
                warnings.append(
                    f"核心记忆有 {cm['unfilled_blocks']} 块仍是出厂占位文本（从未填写）："
                    "这不是「旧」，是「空」，两者要做的事不同 —— 前者去核对，后者去填"
                )
            if cm["unparsable_blocks"]:
                warnings.append(
                    f"核心记忆有 {cm['unparsable_blocks']} 块的时间戳解析不了，"
                    "已按超期计入 —— 时间戳坏掉和真的很旧在判据上不许混为「正常」"
                )
        except Exception as e:
            probes["core_memory_stale"] = None
            probes["core_memory_error"] = str(e)[:120]

        # 事实库与容量水位探针
        facts_count = 0
        try:
            from ducky.utils import get_facts_conn
            conn_f = get_facts_conn()
            facts_count = conn_f.execute("SELECT COUNT(*) FROM facts WHERE archived=0").fetchone()[0]
            conn_f.close()
            probes["facts_active_count"] = int(facts_count)
            # 水位预警（v20.1 WP-B）：阈值从硬编码常数改为可配置，
            # 默认 800 与 v20.0.1 行为逐字节一致 —— 配置化是给依据的通道，
            # 不是调大消音的通道。生效值必须进探针：配置写了不等于生效，
            # 让 /health 自己报出它真用的数，验收问它不问文件。
            # /health 必须永远能应答，所以显式值非法时不抛 —— 报警进探针
            # （facts_watermark_config_error）后回退默认，绝不安静吞掉。
            _wm_raw = os.environ.get("AIDUMEI_FACTS_WATERMARK")
            watermark_threshold = 800
            if _wm_raw is not None:
                try:
                    _wm_val = int(_wm_raw)
                    if _wm_val <= 0:
                        raise ValueError("必须为正整数")
                    watermark_threshold = _wm_val
                except (ValueError, TypeError):
                    probes["facts_watermark_config_error"] = (
                        f"AIDUMEI_FACTS_WATERMARK 值无效: {_wm_raw!r}（需正整数），已回退默认 800"
                    )
            probes["facts_watermark_effective"] = watermark_threshold
            if facts_count > watermark_threshold:
                warnings.append(f"事实库水位较高（当前有效事实 {facts_count} 条，阈值 {watermark_threshold}），建议触发 refine_memory 归档精炼")
                probes["watermark_warning"] = True
            else:
                probes["watermark_warning"] = False
        except Exception as e:
            probes["facts_active_count"] = -1
            probes["facts_error"] = str(e)[:120]
            # v20.1 整改轮（R-07 · 外审 z P2-03）：facts 探测失败时水位
            # 探针不许整体缺席 —— 读取方按键取值会 KeyError，且「没报」
            # 和「没量」必须能区分开。显式 unknown，绝不冒充 False。
            probes.setdefault("facts_watermark_effective", "unknown")
            probes.setdefault("watermark_warning", None)

        # 📼 原文保真层探针（v19.4.0 明镜工程 Phase 1）
        try:
            from ducky.verbatim_vault import count_verbatim_all
            probes["verbatim_count"] = int(count_verbatim_all())
            probes["verbatim_ok"] = True
        except Exception as e:
            probes["verbatim_ok"] = False
            probes["verbatim_error"] = str(e)[:120]

        # 原味抽屉探针（前端 STORAGE LAYERS 长期引用但后端从未产出，此处接上）
        try:
            from ducky.utils import get_text_conn
            conn_r = get_text_conn()
            n_raw = conn_r.execute(
                "SELECT COUNT(*) FROM memories WHERE id LIKE 'raw-%'"
            ).fetchone()[0]
            conn_r.close()
            probes["raw_drawer_count"] = int(n_raw)
            probes["raw_drawer_ok"] = True
        except Exception as e:
            probes["raw_drawer_ok"] = False
            probes["raw_drawer_error"] = str(e)[:120]

        # 🔐 鉴权门禁探针（P0-1/P1-4 v19.4.1）：让部署方一眼看清
        #    「我的记忆库现在到底有没有门禁」。此前 UI 口令与 API token
        #    两套凭据互斥，部署方无从判断自己是被保护还是在裸奔。
        #    只暴露「是否启用/启用来源」，绝不吐任何凭据内容。
        try:
            import os as _os
            from ducky.security.auth import active_session_count, password_source
            _tok = bool(_os.environ.get("AIDUMEM_API_TOKEN", "").strip())
            _pwd_src = password_source()
            _pwd_explicit = bool(_os.environ.get("AIDUMEM_UI_PASSWORD", "").strip()) or _pwd_src == "user"
            probes["auth_gate_enabled"] = bool(_tok or _pwd_explicit)
            probes["auth_api_token_set"] = _tok
            probes["auth_ui_password"] = _pwd_src or "unset"
            probes["auth_active_sessions"] = active_session_count()
            probes["auth_ok"] = True
        except Exception as e:
            probes["auth_ok"] = False
            probes["auth_error"] = str(e)[:120]

        # 🔍 召回路径探针（P1-2/P1-4）：中文查询到底走了 FTS 索引还是 LIKE 全表扫。
        #    v19.4.0 之前中文恒落 LIKE 而文档宣称 trigram 索引，
        #    部署方完全无从察觉。这里用一个探测查询把真实路径暴露出来。
        try:
            from ducky.text_fts import fts_is_authoritative, fts_match_terms
            probes["fts_chinese_indexed"] = bool(fts_match_terms("记忆引擎"))
            probes["fts_authoritative_empty"] = bool(fts_is_authoritative("记忆引擎"))
            probes["fts_terms_ok"] = True
        except Exception as e:
            probes["fts_terms_ok"] = False
            probes["fts_terms_error"] = str(e)[:120]

        # 汇总所有降级组件（全量扫描 module_ok 与 probes 中 _ok=False 项）
        degraded = [k for k, v in module_ok.items() if not v]
        for p_key, p_val in probes.items():
            if p_key.endswith("_ok") and not p_val:
                probe_comp = p_key[:-3]
                if probe_comp not in degraded:
                    degraded.append(probe_comp)

        # 合并动态降级追踪器记录的事件
        for active_deg in DegradationTracker.get_degraded_summary():
            if active_deg not in degraded:
                degraded.append(active_deg)

        # 裸奔告警（P0-1）：门禁未启用时明确写进 warnings，
        # 不让「以为设了密码就安全」的部署方继续误会。
        if probes.get("auth_ok") and not probes.get("auth_gate_enabled"):
            warnings.append(
                "auth_gate_disabled: REST 接口无鉴权。设置 AIDUMEM_API_TOKEN "
                "或通过控制台设置访问口令后门禁生效（仅回环访问时可接受）。"
            )

        status = "ok" if not degraded else "degraded"

        return te_ok(
            service=f"aiduMEM-v{_version_info['service_version']}",
            version=f"{_version_info['service_version']}",
            codename=_version_info["codename"],
            codename_zh=_version_info["codename_zh"],
            modules=module_ok,
            probes=probes,
            degraded=degraded,
            degraded_details=DegradationTracker.get_degraded_details(),
            warnings=warnings,
            health_status=status,
        )

    @app.get("/metrics")
    def metrics(days: int = 7):
        """运行时指标端点。"""
        out: dict = {"version": f"{_version_info['service_version']}"}
        try:
            from ducky.utils import get_facts_conn
            conn = get_facts_conn()
            out["facts_total"] = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            out["facts_active"] = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE archived=0"
            ).fetchone()[0]
            conn.close()
        except Exception as e:
            out["facts_error"] = str(e)[:120]
        try:
            from ducky.salience.metrics import get_historical_metrics
            out["salience_history"] = get_historical_metrics(days)
        except Exception as e:
            out["salience_error"] = str(e)[:120]
        try:
            from ducky.utils import get_text_conn
            c = get_text_conn()
            out["fts_indexed"] = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            c.close()
        except Exception as e:
            out["fts_error"] = str(e)[:120]
        return te_ok(**out)

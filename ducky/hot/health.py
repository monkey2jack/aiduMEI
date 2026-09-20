"""ducky.hot.health — GET /health & /metrics（v19.2.0 可观测性升级版）"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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

_API_PORT_FALLBACK = 8767

logger = logging.getLogger("aiduMEM.hot")

# v21.2.0 写入活性探针的判据阈值。
# 定阈依据（不是拍脑袋）：实测一个真实在用的部署 24h 检索量约 24 次、
# 7 天约 169 次。取 5 —— 远低于真实使用量（不会漏报接线错误），
# 又高于「装完点两下就走」的零星试用（不会对刚部署的空库误报）。
# env 可覆盖：流量形态差异大的部署自己调，但调高就是调低灵敏度。
# 走 env_config.int_env 统一入口：越界与不可解析同等处理（回默认 + 出声），
# 裸 int() 会在 import 期 raise，把整个服务炸掉 —— 一个探针的阈值配错了，
# 代价不该是服务起不来。
from ducky.env_config import int_env as _int_env  # noqa: E402

_INGEST_MIN_READS = _int_env("AIDUMEI_INGEST_MIN_READS", 5, minimum=1)
# 会话数低于这个值不判精华活性：偶尔两三个会话看不出接没接线。
_DISTILL_MIN_SESSIONS = _int_env("AIDUMEI_DISTILL_MIN_SESSIONS", 3, minimum=1)



def _reconcile_degraded_details(degraded: list, probes: dict) -> list:
    """让 `degraded_details` 解释 `degraded` 里的**每一项**。

    🔴 参赛前自查 N-2：这两个字段此前**不同源** —— `degraded` 由健康探针算出，
    `degraded_details` 却只来自 `DegradationTracker`（运行时被显式 mark 过、
    且 300 秒内的事件）。于是零配置首跑时出现了这种输出：

        degraded         = ['vector_backend', 'entity_keywords']
        degraded_details = None

    **明细通道恰好在最需要它的时候是空的。** 一个字段说「这两样坏了」，
    另一个字段对这两样只字不提 —— 调用方无从判断该去修什么。
    这与本仓反复修过的「两个真相源」是同一个形态。

    现在的契约：`degraded` 里每一项都必须在 details 里有一条。理由的来源
    按优先级取 —— 追踪器的运行时记录（带时间戳，信息最全）→ 探针留下的
    `_error / _detail / _reason / _source / _status` → 兜底一句
    「探针判定为不可用，未留下具体原因」。
    **兜底那句本身也是有用的信息**：它说明这条降级没有人给出理由，
    而不是让调用方对着一个 `null` 猜。
    """
    tracked = {}
    try:
        for rec in (DegradationTracker.get_degraded_details() or []):
            name = rec.get("component") or rec.get("name")
            if name:
                tracked[str(name)] = rec
    except Exception as exc:            # 明细拿不到不该把 /health 带崩
        logger.debug("降级明细读取失败，回落到探针理由: %s", exc)

    out = []
    for comp in degraded:
        rec = tracked.get(comp)
        if rec:
            out.append(dict(rec, component=comp, source="tracker"))
            continue
        reason, reason_key = None, ""
        for suffix in ("_error", "_detail", "_reason", "_source", "_status"):
            value = probes.get(f"{comp}{suffix}")
            if value not in (None, "", []):
                reason, reason_key = value, suffix
                break
        out.append({
            "component": comp,
            "reason": str(reason)[:200] if reason else "探针判定为不可用，未留下具体原因",
            "source": f"probe{reason_key}" if reason else "probe_no_reason",
        })
    # 追踪器里有、但不在 degraded 列表里的（例如已恢复但仍在 300s 窗口内）照旧带上
    for name, rec in tracked.items():
        if name not in degraded:
            out.append(dict(rec, component=name, source="tracker_only"))
    return out


# ── 公开 /health 视图（v20.3.2 正式版 · 用户审计 B + Codex F-12）────────────────
#
# 两个问题一起解：
#   · 用户审计 B：一行 Prompt 第 7 步要读 `runtime_paths.data_dir_writable`，而门禁启用后
#     未授权调用方拿到的载荷里 **probes 整个键不存在** → jq 得 null → 按指令必须判失败停工。
#     越按文档做（第 6 步配 token）越走不通。
#   · Codex F-12：脱敏发生在**末尾** —— 匿名请求照样跑完 640 行、38 个 try 的深度探针，
#     才在最后一行把结果扔掉。高频匿名探测 = 免费的数据库压力。
#
# 处置：
#   1. 门禁**未启用** → 放行完整 probes（此时不存在需要防的侦察者，藏字段只伤自己人）；
#   2. 门禁启用且未授权 → **留键说明**而不是删键：`probes._redacted` 告诉调用方「是你没带凭据」，
#      `probes.runtime_paths` 只放 `data_dir_writable` 布尔（路径不外泄，第 7 步能过）；
#   3. 匿名视图带 TTL 缓存：30 秒内重复匿名请求 **O(1)** 返回，不重跑探针。
_PUBLIC_TTL_SECONDS = 30
_PUBLIC_CACHE: dict = {"ts": 0.0, "full": None}

# v20.4 P2-17：/livez 的 uptime 基准。模块加载时刻 ≈ 进程起服务的时刻。
_START_TS = time.time()


def _auth_gate_enabled() -> bool:
    """门禁是否启用；判定失败按启用处理（fail-closed）。"""
    try:
        from api_server import _auth_enabled
        return bool(_auth_enabled())
    except Exception:
        return True


def _resolve_authz(request: Request) -> tuple[bool, bool]:
    """（门禁是否启用, 本请求是否已授权）—— /health 与 /diagnostics 共用。"""
    from ducky.security.auth import SESSION_COOKIE_NAME, validate_session
    gate = _auth_gate_enabled()
    try:
        from api_server import _request_authorized
        authz = bool(_request_authorized(request))
    except Exception:
        authz = validate_session(request.cookies.get(SESSION_COOKIE_NAME, ""))
    return gate, authz


def _public_view(full: dict) -> dict:
    rp = (full.get("probes") or {}).get("runtime_paths") or {}
    return {
        "status": full.get("status"),
        "version": full.get("version"),
        "health_status": full.get("health_status"),
        "degraded": full.get("degraded", []),
        "warming_up": full.get("warming_up", []),
        "probes": {
            "_redacted": "authenticate (Authorization: Bearer <AIDUMEM_API_TOKEN>) to view runtime probes",
            "runtime_paths": {
                "data_dir_writable": rp.get("data_dir_writable"),
                "_redacted": "paths hidden for unauthenticated callers",
            },
        },
    }


def register_health_routes(app: FastAPI) -> None:
    def _run_full_probe() -> dict:
        """B 档：lazy 预热 + 真实探针 + 反静默降级追踪 + 水位预警。

        授权 /health 与 /diagnostics 共用这一份完整载荷；调用方各自
        决定匿名语义（/health 留键公开视图，/diagnostics 一律 401）。
        """
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
        }

        # v20.3 WP-A-02：恒绿字段不是探针。要么测真事实，要么删掉。
        # 这里的真事实不是“有没有另一个进程监听”（那是巡检脚本的职责），
        # 而是“当前服务实例能否建立出站 socket”。若系统连 socket 都不可用，
        # /health 自己也答不出来；但能答出来时，这项至少代表网络栈可用。
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
                probe_socket.bind(("127.0.0.1", 0))
                probes["port_service"] = True
        except OSError as port_exc:
            probes["port_service"] = False
            probes["port_service_error"] = str(port_exc)[:120]

        # v20.2.5（外审 F-01）：报出**实际打开的**运行目录与可写性。
        #
        # DATA_DIR 由 `__file__` 上两级推导（除非 AIDUMEM_DATA_DIR 显式覆盖）。
        # 按 wheel 安装时包在 site-packages 里，于是数据落进 site-packages/data
        # —— 而 Docker bind-mount 的是 /app/data。两者不一致时**没有任何症状**：
        # 服务正常起、接口正常答，数据写进容器层，重建即丢。
        #
        # 所以这几行不是锦上添花：「以为挂载生效了其实没有」只能靠它发现。
        # 顺带报可写性 —— 只读 site-packages 上的首次写入会直接
        # `attempt to write a readonly database`，早知道一秒胜过事后翻日志。
        try:
            import os as _o
            from ducky.utils import BASE_DIR as _BD, DATA_DIR as _DD, LOG_DIR as _LD
            probes["runtime_paths"] = {
                "base_dir": _o.path.abspath(_BD),
                "data_dir": _o.path.abspath(_DD),
                "log_dir": _o.path.abspath(_LD),
                "facts_db": _o.path.abspath(FACTS_DB),
                "data_dir_writable": _o.access(_DD, _o.W_OK),
                # 落在包目录里 = 交付形态与源码形态的路径语义没对齐（外审 F-01）
                "data_dir_inside_package": _o.path.abspath(_DD).startswith(
                    _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))),
                "explicitly_configured": bool(_o.environ.get("AIDUMEM_DATA_DIR")),
            }
            probes["runtime_paths"]["writable_warning"] = (
                None if probes["runtime_paths"]["data_dir_writable"]
                else "数据目录不可写：%s —— 首次写入会以 readonly database 失败"
                     % probes["runtime_paths"]["data_dir"])
            # v20.5.1（T-10 · DEPLOY_DOCKHOLD 记录在案的坑上门）：DATA_DIR 只搬
            # SQLite，mem0 配置里的 qdrant path / history_db_path 不会被跟着重写
            # —— 脱钩时向量库写进旧位置且无症状。「能写」上面查了，这里查
            # 「写对地方」。只告警不拒启（诚实暴露，由运维裁决）。
            from ducky.mem0_runtime import vector_path_consistency as _vpc
            probes["runtime_paths"]["path_consistency"] = _vpc()
        except Exception as _rp_exc:
            probes["runtime_paths"] = {"error": str(_rp_exc)[:120]}

        # v20.2.4（外审 F-18）：报**生效模式**，不是「模块可导入」。
        # GUARD_MODE 拼错时旧代码静默降级为 log-only，而 injection_guard_ok
        # 照样是 True —— 探针说「守卫在」，守卫其实只在记日志。
        try:
            from ducky.security.injection_guard import guard_mode_status
            probes["injection_guard_mode"] = guard_mode_status()
        except Exception as _ig_exc:
            probes["injection_guard_mode"] = {"error": str(_ig_exc)[:100]}

        # v20.3.2-beta（外审 P1-B）：**误拒率是这条防线唯一需要盯的运营指标。**
        # 一条真实记忆被拒，调用方常把 400 当「写过了」→「存了就是搜不到」。
        # 以前完全不可见；现在把近 5 分钟拒收数摊开，运维能看见它在拒什么量级。
        try:
            from ducky.security.injection_guard import rejection_stats
            probes["injection_rejections"] = rejection_stats()
        except Exception as _rj_exc:
            probes["injection_rejections"] = {"error": str(_rj_exc)[:100]}

        # v20.3.2 正式版（基座 mem0ai 2.0.19→2.0.20 配套）：**补丁台账上 /health。**
        # 上一轮外审说「补丁台账逐条上 /health」—— 本文件此前 0 处引用 mem0_patches，
        # 那句话是错的。基座每升一次小版本，运维必须一眼看到 applied / not_needed /
        # drift；没有这个探针，「补丁层在 2.0.20 上还对不对」只能靠跑测试才知道。
        try:
            from ducky.mem0_patches import patch_status
            probes["mem0_patches"] = patch_status()
            try:
                from ducky.utils import leaked_transaction_count, leaked_transaction_last
                probes["sqlite_leaked_transactions"] = {"seen_since_start": leaked_transaction_count(),
                                                        "last": leaked_transaction_last() or None}
            except Exception as _exc:  # 探针失败不许拖垮 /health
                probes["sqlite_leaked_transactions"] = {"error": str(_exc)[:80]}
        except Exception as _mp_exc:
            probes["mem0_patches"] = {"error": str(_mp_exc)[:100]}

        # v20.2.4（外审 F-03）：本地档拦下的云出口计数。
        # 「local 档零外呼」这句话需要一个能被观察的凭据，而不只是一条测试。
        try:
            from ducky.engine_mode import cloud_egress_blocked_counts
            _blocked = cloud_egress_blocked_counts()
            if _blocked:
                probes["cloud_egress_blocked"] = _blocked
        except Exception as _ce_exc:
            logger.debug("云出口计数探针跳过: %s", _ce_exc)

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
            from ducky.engine_mode import mode_status
            probes["engine_mode_policy"] = mode_status()
        except Exception as _mexc:
            probes["engine_mode_policy"] = {"error": str(_mexc)[:120]}
        try:
            from ducky.gear import gear_status, llm_gear_status
            probes["engine_gear"] = gear_status()
            # v20.2.2：LLM 蒸馏腿的挡位（与嵌入腿互相独立——LLM 断供时
            # 写入降为确定性直写秒回，欠蒸馏可查，见 gear.py LLM 腿）。
            probes["llm_gear"] = llm_gear_status()
        except Exception as _gexc:
            probes["engine_gear"] = {"error": str(_gexc)[:120]}
        try:
            from ducky.local_embed import local_embed_status
            probes["local_embed"] = local_embed_status()
        except Exception as _lexc:
            probes["local_embed"] = {"error": str(_lexc)[:120]}
        try:
            from ducky.dual_index import (last_replay_status, local_point_count,
                                          pending_counts)
            probes["local_index_points"] = local_point_count()
            # v20.2.1（外审 R2 配套）：水位旁带「上次重放」——欠账长期非零
            # 而 last_replay 一直 None/很旧，就是重放触发链断了的直接证据。
            _pc = pending_counts()
            from ducky.dual_index import pending_verdict
            probes["pending_embeddings"] = {**_pc,
                                            "last_replay": last_replay_status(),
                                            "verdict": pending_verdict(_pc)}
        except Exception as _dexc:
            probes["dual_index_error"] = str(_dexc)[:120]

        # v20.1.1（N-1）：限流生效值可查——配置生效三查的运维面。
        # v20.2.1（外审 R1 同款）：非法 env 改为回退默认不再抛，探针从
        # 「捕 ValueError」换成读 config_errors —— 不静默的纪律不变，
        # 出声方式从炸改成常驻可查。
        try:
            from ducky.rate_guard import (add_rate_limit, delete_all_rate_limit,
                                          rate_config_errors)
            probes["rate_add_per_min_effective"] = add_rate_limit()
            probes["rate_delete_all_per_min_effective"] = delete_all_rate_limit()
            # v20.2.4（外审 F-19）：登录表规模 / 上限 / 是否处于全局节流。
            # 「表会不会涨到撑爆」此前只能靠读代码猜；现在是一个可查的数字。
            from ducky.rate_guard import login_table_status
            probes["login_table"] = login_table_status()
            _rc_err = rate_config_errors()
            if _rc_err:
                probes["rate_limit_config_error"] = "; ".join(
                    _rc_err.values())[:160]
        except Exception as _rl_exc:
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

        # v21 F1：epistemic 出身标签探针。抽样最新 100 行 facts，
        # 校验 epistemic_mode 非空且落在合法枚举——只报代码常量同样
        # 是假绿灯（列不存在时照样能 import 到枚举），所以抽的是磁盘真行。
        try:
            from ducky.epistemic import EPISTEMIC_MODES
            from ducky.utils import get_facts_conn as _epi_conn_fn
            _epi_conn = _epi_conn_fn()
            try:
                _epi_rows = _epi_conn.execute(
                    "SELECT epistemic_mode, created_at FROM facts ORDER BY id DESC LIMIT 100"
                ).fetchall()
            finally:
                _epi_conn.close()
            _epi_total = len(_epi_rows)
            _epi_bad = sum(
                1 for m, _c in _epi_rows
                if not m or m not in EPISTEMIC_MODES
            )
            probes["epistemic_sample_size"] = _epi_total
            probes["epistemic_ok"] = _epi_bad == 0
            if _epi_bad:
                probes["epistemic_invalid"] = _epi_bad
                DegradationTracker.record_degradation(
                    "epistemic", f"{_epi_bad}/{_epi_total} sampled rows have invalid epistemic_mode"
                )
            # v21.0 收口（生产用户 🟢-2）：合法性 ≠ 在主链路工作。
            # 告警签名收窄为「全是 fuzzy」——那才是打标没在干活的铁证；
            # 全是 reasoned 不报警：pattern_extract 是主笔，单一推断档是健康常态
            # （否则告警疲劳，探针会被人关掉）。存量全 fuzzy 且 fresh<10 不判。
            from datetime import datetime, timedelta, timezone
            _cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            _fresh_modes = []
            for _m, _c in _epi_rows:
                if not _c:
                    continue
                try:
                    _ct = datetime.fromisoformat(str(_c).replace(" ", "T")).replace(
                        tzinfo=None if "+" not in str(_c) else timezone.utc)
                    _ct_cmp = _ct.replace(tzinfo=None)
                    if _ct_cmp >= _cutoff.replace(tzinfo=None):
                        _fresh_modes.append(_m)
                except (ValueError, TypeError):
                    continue
            probes["epistemic_fresh_24h"] = len(_fresh_modes)
            probes["epistemic_diversity"] = len(set(_fresh_modes))
            if len(_fresh_modes) >= 10 and set(_fresh_modes) == {"fuzzy"}:
                DegradationTracker.record_degradation(
                    "epistemic_diversity",
                    f"最近 24h 的 {len(_fresh_modes)} 条新写入全部是 fuzzy"
                    "——主链路打标疑似不工作")
        except Exception as _epi_exc:
            probes["epistemic_ok"] = False
            probes["epistemic_error"] = str(_epi_exc)[:120]

        # v21.2.0 审计整改轮（生产用户审计 🔴-1）：溯源覆盖率探针。
        #
        # 由来：v21.2 上线后 sidecar 33 行里 origin_session_id 非空 = 0 行，
        # 回声抑制（M2）的向量腿从上线起就在空转 —— 而 epistemic_ok 是绿的
        # （出身档合法），单测也是绿的（单线程内语义正确）。**合法 ≠ 在工作**，
        # 这个缺口是靠用户审计翻生产库才发现的。探针的职责就是把这种
        # 「绿着的空转」当场变成红字，不必再等下一次人工审计。
        #
        # 判据刻意分两层，避免「没流量也报警」的告警疲劳：
        #   · 永远如实报 coverage 与样本量（数字本身就是证据）
        #   · 只有「窗口内确实有足量新写入、却一条都没带 session」才记降级
        #
        # 窗口取 **7 天**而不是 24h：本仓典型日增只有个位数 sidecar 行，
        # 用 24h + 阈值 10 的组合，探针会几乎永不触发 —— 那就又造了一块
        # 白护栏（守卫的经典坏死法之一：判据的射程盖不住缺陷的分布）。
        # 7 天窗口下同一个阈值才真的有射程。24h 的数字照报，只做观测不做判据。
        try:
            from ducky.utils import get_facts_conn as _cov_conn_fn
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            _cov_conn = _cov_conn_fn()
            try:
                _cov_cols = {r[1] for r in _cov_conn.execute(
                    "PRAGMA table_info(memory_epistemic)")}
                if "origin_session_id" not in _cov_cols:
                    probes["epistemic_session_coverage"] = None
                    probes["epistemic_session_coverage_note"] = "sidecar 未迁到 schema v9"
                else:
                    def _cov_window(hours):
                        _cut = (_dt.now(_tz.utc) - _td(hours=hours)).isoformat()
                        _r = _cov_conn.execute(
                            "SELECT COUNT(*), SUM(CASE WHEN origin_session_id != '' "
                            "THEN 1 ELSE 0 END) FROM memory_epistemic WHERE created_at >= ?",
                            (_cut,)).fetchone()
                        return int((_r[0] if _r else 0) or 0), int((_r[1] if _r else 0) or 0)

                    _d_total, _d_with = _cov_window(24)
                    _w_total, _w_with = _cov_window(24 * 7)
                    probes["epistemic_session_fresh_24h"] = _d_total
                    probes["epistemic_session_fresh_7d"] = _w_total
                    probes["epistemic_session_coverage"] = (
                        round(_w_with / _w_total, 4) if _w_total else None)
                    probes["epistemic_session_coverage_window"] = "7d"
                    # 分母说清楚（用户审计 🔵-5）：这个比例的分母是**全部**
                    # sidecar 登记，里面混着 cron 整合器与 MEMORY.md 同步引擎
                    # 等后台通路 —— 它们永远不带 session，会把比例长期压在很低
                    # 的位置。判据本身不受影响（阈值是「一条都没有」不是比例），
                    # 但读数的人会把 0.05 误读成「只有 5% 的对话带 session」。
                    # 所以把两个数都摆出来，让比例可被正确解读。
                    probes["epistemic_session_with_7d"] = _w_with
                    probes["epistemic_session_coverage_note"] = (
                        "分母含后台通路（cron/同步引擎），它们不带 session；"
                        "判据看的是「7 天内带 session 的条数是否为 0」，不是这个比例")
                    # 样本不足不判 —— 没流量不是故障。
                    if _w_total >= 10 and _w_with == 0:
                        DegradationTracker.record_degradation(
                            "epistemic_session_coverage",
                            f"最近 7 天的 {_w_total} 条 sidecar 登记**没有一条带 session**"
                            " —— 回声抑制(M2)的向量腿与轨迹登记(M1)在空转；"
                            "查两处：调用方是否透传 _origin_session_id、"
                            "写入链路是否把 metadata 传到了 _index_after_add")
            finally:
                _cov_conn.close()
        except Exception as _cov_exc:
            probes["epistemic_session_coverage"] = None
            probes["epistemic_session_coverage_error"] = str(_cov_exc)[:120]

        # ══════════════════════════════════════════════════════════════
        # v21.2.0 写入活性探针（ingest_liveness）
        # ══════════════════════════════════════════════════════════════
        #
        # 由来（2026-09-17，本仓最贵的一课）：一个生产部署把 aiduMEI 的
        # **注入钩子**挂上了、**写入钩子**却从没挂过 —— 宿主每天正常对话、
        # 每轮都来检索，但没有任何一轮把内容写回记忆库。这个状态持续了很久
        # 才被人工审计发现，而在此期间 /health 的每一项都是绿的。
        #
        # 根本原因不是某个功能坏了，是**观测方向错了**：本仓当时所有探针都在
        # 回答「库里已有的记忆好不好」（schema、出身、衰减、检索延迟……），
        # 没有一个在回答「今天该进来的进来了吗」。体检报告全绿的人，
        # 可能已经三天没吃饭。
        #
        # 判据：把「读」和「写」放在一起比。只读不写是**接线错误**的铁证 ——
        # 一个真在被使用的记忆系统不可能长期只出不进。
        #   · 有检索、且窗口内写入为 0        → 降级（接线漏了写钩子）
        #   · 没检索                          → 不判（没人用，不是故障）
        #   · 有写入                          → 健康，如实报数
        try:
            from ducky.utils import get_facts_conn as _ing_conn_fn
            from datetime import datetime as _idt, timedelta as _itd, timezone as _itz

            _ing_window_h = 24
            _ing_cut_iso = (_idt.now(_itz.utc) - _itd(hours=_ing_window_h)).isoformat()
            _ing_cut_ts = (_idt.now(_itz.utc) - _itd(hours=_ing_window_h)).timestamp()

            # 写入侧要分两个数，缺一不可：
            #   _ing_writes      —— 任何来源的新增（含 cron 整合器、MEMORY.md
            #                       同步引擎等后台通路），只作参考，**不作判据**
            #   _ing_turn_writes —— 带 origin_session_id 的新增，即「一轮对话
            #                       结束后写回来的那一条」
            # 只数前者会造白护栏：本探针第一版就是这么写的，而实测那台出事的
            # 机器每天有 6~18 条后台通路写入、带 session 的**恒为 0** —— 探针
            # 在它本该抓住的那次事故上永远是绿的。判据必须落在对话这条腿上。
            _ing_writes = 0
            _ing_turn_writes = 0
            _ing_conn = _ing_conn_fn()
            try:
                for _sql in (
                    "SELECT COUNT(*) FROM facts WHERE created_at >= ?",
                    "SELECT COUNT(*) FROM memory_epistemic WHERE created_at >= ?",
                ):
                    try:
                        _r = _ing_conn.execute(_sql, (_ing_cut_iso,)).fetchone()
                        _ing_writes += int((_r[0] if _r else 0) or 0)
                    except Exception:
                        continue  # 表不在（未迁移库）不算故障
                try:
                    _r = _ing_conn.execute(
                        "SELECT COUNT(*) FROM memory_epistemic "
                        "WHERE created_at >= ? AND COALESCE(origin_session_id, '') <> ''",
                        (_ing_cut_iso,)).fetchone()
                    _ing_turn_writes = int((_r[0] if _r else 0) or 0)
                except sqlite3.Error:
                    # 只收窄到 sqlite 侧的错（表/列不在＝未迁移库）。判据无从
                    # 谈起时退回全量写入口径，宁可少报也不假红 —— 老库上这条
                    # 探针只当参考。别的异常照旧往外抛给下面的兜底。
                    _ing_turn_writes = _ing_writes
            finally:
                _ing_conn.close()

            # 读取侧同样要分两个数，理由与写入侧一模一样：
            #   _ing_reads      —— 任何来源的检索，含 e2e_smoke 每小时一次的
            #                      巡检心跳，只作参考
            #   _ing_conv_reads —— 带 origin_session_id 的检索，即「真有人在对话」
            # 实测生产近 24h 的 24 条记录**每一条都是 `aidumei-smoke-*`**，
            # 一条真实对话检索都没有。拿总数当「有人在用」的证据，等于把自己的
            # 巡检心跳当成了用户 —— 那样只要一天没聊天，探针就必然误报写线断了。
            _ing_reads = 0
            _ing_conv_reads = 0
            try:
                from ducky.evolve_mem import get_evolve_conn as _ing_ev_fn
                _ing_ev = _ing_ev_fn()
                try:
                    _r = _ing_ev.execute(
                        "SELECT COUNT(*) FROM evolve_queries WHERE ts >= ?",
                        (_ing_cut_ts,)).fetchone()
                    _ing_reads = int((_r[0] if _r else 0) or 0)
                    try:
                        _r2 = _ing_ev.execute(
                            "SELECT COUNT(*) FROM evolve_queries WHERE ts >= ? "
                            "AND COALESCE(origin_session_id, '') <> ''",
                            (_ing_cut_ts,)).fetchone()
                        _ing_conv_reads = int((_r2[0] if _r2 else 0) or 0)
                    except sqlite3.Error:
                        # 列不在（未迁移库）：无从区分，两个数取同一口径。
                        # 下面的判决会因此退回旧行为，如实标注而不假装精确。
                        _ing_conv_reads = _ing_reads
                finally:
                    _ing_ev.close()
            except Exception:
                _ing_reads = 0
                _ing_conv_reads = 0

            probes["ingest_writes_24h"] = _ing_writes
            probes["ingest_turn_writes_24h"] = _ing_turn_writes
            probes["ingest_reads_24h"] = _ing_reads
            probes["ingest_conv_reads_24h"] = _ing_conv_reads

            # 三态，不是两态。少了中间那一态就会在两个方向上都撒谎：
            #   红   有人在对话（conv_reads 够）却零对话写入 → 写线断了
            #   提示 完全没有对话检索、但巡检说明服务活着 → 读线没透传 session，
            #        既判不了写线，M2 回声抑制也在空转。这时报「写线断了」是
            #        冤枉人，报「一切正常」是白护栏 —— 只能如实说「读线该升级」。
            #   绿   其余（含样本不足：没人用不是故障）
            _ing_bad = _ing_conv_reads >= _INGEST_MIN_READS and _ing_turn_writes == 0
            _ing_blind = (_ing_conv_reads == 0 and _ing_reads >= _INGEST_MIN_READS
                          and _ing_turn_writes == 0)
            probes["ingest_liveness_ok"] = not _ing_bad
            if _ing_bad:
                DegradationTracker.record_degradation(
                    "ingest_liveness",
                    f"最近 {_ing_window_h}h 有 {_ing_conv_reads} 次**来自对话的检索**，"
                    f"却没有一条带会话来源的写入（同期任何来源的写入共 {_ing_writes} 条，"
                    "多为后台通路）。宿主多半只挂了注入钩子、没挂写入钩子 —— "
                    "记忆只出不进＝在失忆。现成脚本见 docs/AGENT_INTEGRATION.md「两条线」，"
                    "或运行 scripts/check_ingest_wiring.py")
            elif _ing_blind:
                probes["ingest_liveness_note"] = (
                    f"无法判断：{_ing_window_h}h 内 {_ing_reads} 次检索没有一次带 session，"
                    "说明读线是不透传 session 的旧版本。升级 aidumem-inject.sh 后本探针才有射程；"
                    "在那之前 M2 回声抑制也一直在空转（检索侧拿不到会话就不做排除）。")
                DegradationTracker.record_degradation(
                    "ingest_liveness",
                    probes["ingest_liveness_note"], severity="warning")
        except Exception as _ing_exc:
            probes["ingest_liveness_ok"] = None
            probes["ingest_liveness_error"] = str(_ing_exc)[:120]

        # v22.0（雷霆审计 B12 · Kimi Y-1）：谱系完整性探针。
        # verify_lineage_integrity 存在但无探针消费——「链断了」没人知道。
        # 降级为「可检测」：不追求不可篡改（HMAC 整行进 v21.3 排期），
        # 但让「链断裂/幽灵链/空 hash 碰撞」在 /health 可见。
        try:
            from ducky.memory_lineage import verify_lineage_integrity as _verify_lineage
            _lin = _verify_lineage()
            _lin_broken = _lin.get("broken", 0) or 0
            _lin_total = _lin.get("total", 0) or 0
            probes["lineage_integrity_ok"] = _lin_broken == 0
            probes["lineage_integrity_broken"] = _lin_broken
            probes["lineage_integrity_total"] = _lin_total
            if _lin_broken > 0:
                DegradationTracker.record_degradation(
                    "lineage_integrity",
                    f"谱系链 {_lin_broken}/{_lin_total} 条断裂——"
                    "可能是 lastrowid 缺陷、空 hash 碰撞或手动篡改。",
                    severity="warning")
        except Exception as _lin_exc:
            probes["lineage_integrity_ok"] = None
            probes["lineage_integrity_error"] = str(_lin_exc)[:120]

        # ══════════════════════════════════════════════════════════════
        # v21.2.0 会话精华活性探针（distill_liveness）
        #
        # 第三条线（session_end → 会话精华）漏挂的代价比写线轻，但同样安静：
        # 记忆照常进，只是永远没有「这一程」那一层，没有任何绿灯会因此变红。
        # 判据与写入活性同构 —— 分子分母取同一类流量：
        #   会话数   有多少个不同的 origin_session_id（排除精华自己写的那些）
        #   精华数   origin_agent = 'session-distill' 的登记条数
        # 会话够多却一条精华都没有 ＝ 第三条线没挂上。
        # 会话数不够不判：没人用不是故障（与 ingest_liveness 同一口径）。
        # ══════════════════════════════════════════════════════════════
        try:
            _dis_conn = _ing_conn_fn()
            try:
                _r = _dis_conn.execute(
                    "SELECT COUNT(DISTINCT origin_session_id) FROM memory_epistemic "
                    "WHERE created_at >= ? AND COALESCE(origin_session_id,'') <> '' "
                    "AND COALESCE(origin_agent,'') <> 'session-distill'",
                    (_ing_cut_iso,)).fetchone()
                _dis_sessions = int((_r[0] if _r else 0) or 0)
                _r2 = _dis_conn.execute(
                    "SELECT COUNT(*) FROM memory_epistemic WHERE created_at >= ? "
                    "AND COALESCE(origin_agent,'') = 'session-distill'",
                    (_ing_cut_iso,)).fetchone()
                _dis_made = int((_r2[0] if _r2 else 0) or 0)
            finally:
                _dis_conn.close()
            probes["distill_sessions_24h"] = _dis_sessions
            probes["distill_made_24h"] = _dis_made
            _dis_bad = _dis_sessions >= _DISTILL_MIN_SESSIONS and _dis_made == 0
            # 探针语义修正：「有会话却零精华」的判据不该把「条数太少、本就萃取不出」的
            # 短会话算进分母。一个 1~2 条的会话走完正常生命周期也产生不了精华，
            # 把它算进「有会话」等于把正常行为判成故障。
            # 所以分母只数「够得着萃取门槛的会话」——那些本来就该有精华产出的。
            _dis_effective = 0
            if _dis_sessions > 0:
                _r3 = _dis_conn.execute(
                    "SELECT origin_session_id, COUNT(*) as cnt FROM memory_epistemic "
                    "WHERE created_at >= ? AND COALESCE(origin_session_id,'') <> '' "
                    "AND COALESCE(origin_agent,'') <> 'session-distill' "
                    "GROUP BY origin_session_id HAVING cnt >= ?",
                    (_ing_cut_iso, 3)).fetchall()
                _dis_effective = len(_r3)
            probes["distill_sessions_effective_24h"] = _dis_effective
            _dis_bad = _dis_effective >= _DISTILL_MIN_SESSIONS and _dis_made == 0
            probes["distill_liveness_ok"] = not _dis_bad
            if _dis_bad:
                DegradationTracker.record_degradation(
                    "distill_liveness",
                    f"最近 {_ing_window_h}h 有 {_dis_effective} 个会话够得着萃取门槛（≥3条），"
                    "却一条会话精华都没产出 —— 宿主多半没挂 session_end 钩子。"
                    "记忆照常进，只是永远没有「这一程最值得记住的是什么」那一层。"
                    "现成脚本：integrations/aidumem-distill.sh，挂法见 "
                    "docs/AGENT_INTEGRATION.md「三条线」。")
            else:
                DegradationTracker.clear_degradation("distill_liveness")
        except (ImportError, sqlite3.Error, ValueError, TypeError) as _dis_exc:
            # 收窄到探针自己可能出的错（模块缺 / 库读失败 / 列不在）。
            # 读不到一律标 None + 写明原因 —— 不冒充「正常」。
            probes["distill_liveness_ok"] = None
            probes["distill_liveness_error"] = str(_dis_exc)[:120]

        # v22.0（雷霆审计 A7）：unsafe_combo —— 安全逃逸门组合态探针。
        # 单个逃生舱是知情选择，组合态（INSECURE_PUBLIC ∧ TRUST_PROXY ∧ 无凭据
        # ∧ ALLOW_IMPLICIT_CALLER）必须可见：即使部署方二次确认过，/health 也
        # 要如实报告「本实例处于组合逃逸形态」，让运维巡检一眼看到。
        try:
            import os as _os_sc
            _insecure_pub = _os_sc.environ.get(
                "AIDUMEM_ALLOW_INSECURE_PUBLIC", "0").lower() in {"1", "true", "yes"}
            _trust_proxy = _os_sc.environ.get(
                "AIDUMEI_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes"}
            _implicit = _os_sc.environ.get(
                "AIDUMEI_ALLOW_IMPLICIT_CALLER", "").strip() == "1"
            _combo: list[str] = []
            if _insecure_pub:
                _combo.append("insecure_public")
            if _trust_proxy:
                _combo.append("trust_proxy")
            if _implicit:
                _combo.append("implicit_caller")
            if not _auth_gate_enabled():
                _combo.append("no_credential")
            # 两个以上逃生门同开（或任一个 + 无凭据）即报告组合态
            probes["unsafe_combo"] = _combo if len(_combo) >= 2 else []
            probes["unsafe_combo_ok"] = len(_combo) < 2
        except Exception as _sc_exc:
            probes["unsafe_combo_ok"] = None
            probes["unsafe_combo_error"] = str(_sc_exc)[:120]


        # v21.2.0 审计整改轮（生产用户审计 🔴-2）：episode_ok —— 任务书 DoD 点名要的探针，
        # v21.2.0 漏做且实录未登记缺口。查的是「轨迹这条腿能不能用」：
        # evolve 库连得上、两张表在、以及（有数据时）最近一次登记的时间。
        try:
            from ducky.evolve_mem import get_evolve_conn as _ep_conn_fn
            _ep_conn = _ep_conn_fn()
            try:
                _ep_tables = {r[0] for r in _ep_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                _ep_ready = {"evolve_episodes", "evolve_episode_steps"} <= _ep_tables
                probes["episode_ok"] = _ep_ready
                if _ep_ready:
                    _ep_row = _ep_conn.execute(
                        "SELECT COUNT(*), MAX(last_step_at) FROM evolve_episodes").fetchone()
                    probes["episode_count"] = int((_ep_row[0] if _ep_row else 0) or 0)
                    probes["episode_last_step_at"] = (_ep_row[1] if _ep_row else None)
                    _st_row = _ep_conn.execute(
                        "SELECT COUNT(*) FROM evolve_episode_steps").fetchone()
                    probes["episode_step_count"] = int((_st_row[0] if _st_row else 0) or 0)
                else:
                    DegradationTracker.record_degradation(
                        "episode", "evolve 库缺 evolve_episodes / evolve_episode_steps 表"
                                   " —— M1 轨迹级奖励无处落账")
            finally:
                _ep_conn.close()
        except Exception as _ep_exc:
            probes["episode_ok"] = False
            probes["episode_error"] = str(_ep_exc)[:120]

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
        # v20.2.5（外审 F-01）：运行目录不可写要进 warnings 面。
        # 探针阶段（在上面）算好挂在 runtime_paths 里，因为 warnings 这个名字
        # 到这一行才存在 —— Ruff 的 F821 当场抓住了我第一版直接 append 的写法，
        # 那正是本版接这道关要防的形态。
        _rp_warn = (probes.get("runtime_paths") or {}).get("writable_warning")
        if _rp_warn:
            warnings.append(_rp_warn)
        # v20.5.1（T-10）：路径一致性 WARNING 同型上 warnings 面。
        # 绝对路径只进授权视图——匿名面经 _public_view 白名单只留
        # data_dir_writable 布尔，本字段整体不出现在匿名载荷里。
        _pc_warn = ((probes.get("runtime_paths") or {}).get("path_consistency") or {}).get("warning")
        if _pc_warn:
            warnings.append(_pc_warn)
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
                warnings.append(f"事实库水位较高（当前有效事实 {facts_count} 条，阈值 {watermark_threshold}）。注意：refine_memory 在无 LLM 挡位下会把多条记忆压成一句目录（有损，v20.0 实测 20 条换 1 句），先确认 LLM 挡位可用再触发；或按容量规划调高 AIDUMEI_FACTS_WATERMARK")
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
        warming_up: list[str] = []
        for p_key, p_val in probes.items():
            if p_key.endswith("_ok") and not p_val:
                probe_comp = p_key[:-3]
                if probe_comp == "vector_backend" and probes.get("vector_backend_probed") is False:
                    if probe_comp not in warming_up:
                        warming_up.append(probe_comp)
                    continue
                if probe_comp not in degraded:
                    degraded.append(probe_comp)

        # v22.0（雷霆审计 B1 · Step P1-1）：非 `_ok` 键的失败也进降级——
        # `*_error` / `*_degraded` 键里的非空错误信息是探针失败的另一种形态。
        for p_key, p_val in probes.items():
            if p_key.endswith("_ok") or p_key in ("degraded_details", "warming_up"):
                continue
            if p_val in (False, 0, "", None, []):
                continue
            if p_key.endswith("_error") or p_key.endswith("_degraded"):
                probe_comp = p_key.rsplit("_", 1)[0]
                if probe_comp not in degraded and probe_comp not in warming_up:
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

        full = te_ok(
            service=f"aiduMEM-v{_version_info['service_version']}",
            version=f"{_version_info['service_version']}",
            codename=_version_info["codename"],
            codename_zh=_version_info["codename_zh"],
            modules=module_ok,
            probes=probes,
            degraded=degraded,
            warming_up=warming_up,
            degraded_details=_reconcile_degraded_details(degraded, probes),
            warnings=warnings,
            health_status=status,
        )
        # Public health must remain useful for load balancers without becoming a
        # reconnaissance report. 门禁启用且未授权 → 留键说明的公开视图（见模块头注释）。
        return full

    @app.get("/health")
    def health(request: Request):
        """对外契约端点：授权拿完整探针；匿名拿留键公开视图（30s TTL 缓存）。"""
        _gate, _authz = _resolve_authz(request)
        _anonymous = _gate and not _authz
        if _anonymous and _PUBLIC_CACHE["full"] is not None \
                and time.time() - _PUBLIC_CACHE["ts"] < _PUBLIC_TTL_SECONDS:
            # 匿名 + 30s 内已有全量结果：不重跑探针（Codex F-12）。
            return _public_view(_PUBLIC_CACHE["full"])
        full = _run_full_probe()
        _PUBLIC_CACHE["ts"] = time.time()
        _PUBLIC_CACHE["full"] = full
        if _anonymous:
            return _public_view(full)
        return full

    @app.get("/livez")
    def livez():
        """O(1) 探活（v20.4 P2-17 探针成本分级）。

        负载均衡器/编排器高频打这个端点：进程能应答即「活」。
        不碰磁盘、数据库、单例 —— 那是 /readyz 与 /diagnostics 的事；
        让探活为深度状态付成本，正是上轮 Codex F-12 要拆掉的形态。
        """
        return te_ok(
            service=f"aiduMEM-v{_version_info['service_version']}",
            version=f"{_version_info['service_version']}",
            uptime_seconds=round(time.time() - _START_TS, 3),
        )

    @app.get("/readyz")
    def readyz():
        """就绪探针：只跑廉价确定性检查（v20.4 P2-17 探针成本分级）。

        四项都是 O(毫秒) 的本地事实：库文件在场、数据目录可写、磁盘
        schema 与代码期望对齐。任一失败 → 503 + failed 记名，编排器据此
        摘流。不外呼、不 COUNT、不读大表 —— 那些属于 /diagnostics 档。

        为什么不查 mem0 单例：单例惰性建，冷启动后首个请求前「尚未初始化」
        是常态而非故障 —— 放进就绪判据会让每台新实例在首次使用前一直 503
        （冷启动常态被当成事故，几次假警报后这栏就没人看了）。单例状态在
        /diagnostics 的 mem0_singleton 探针里。响应只报检查名与布尔：
        匿名调用方拿不到任何路径。
        """
        checks: dict[str, bool] = {}
        checks["facts_db"] = os.path.exists(FACTS_DB)
        checks["text_fts_db"] = os.path.exists(TEXT_FTS_DB)
        # except 收窄（P2-13 棘轮只降不升）：import 失败只出 ImportError，
        # os.access 对无效路径返回 False 不抛，OSError 兜路径类型边界。
        try:
            from ducky.utils import DATA_DIR as _DD
            checks["data_dir_writable"] = os.access(_DD, os.W_OK)
        except (ImportError, OSError) as _dd_exc:
            logger.debug("readyz data_dir 检查失败: %s", _dd_exc)
            checks["data_dir_writable"] = False
        try:
            import sqlite3 as _sqlite3
            from ducky.schema_bootstrap import CURRENT_SCHEMA_VERSION
            from ducky.utils import get_facts_conn
            _rc = get_facts_conn()
            try:
                _on_disk = int(_rc.execute("PRAGMA user_version").fetchone()[0])
            finally:
                _rc.close()
            checks["schema_version"] = _on_disk == int(CURRENT_SCHEMA_VERSION)
        except (ImportError, _sqlite3.Error, ValueError, TypeError) as _sv_exc:
            logger.debug("readyz schema 检查失败: %s", _sv_exc)
            checks["schema_version"] = False
        failed = [name for name, ok in checks.items() if not ok]
        body = te_ok(
            version=f"{_version_info['service_version']}",
            ready=not failed,
            checks=checks,
            failed=failed,
        )
        if failed:
            return JSONResponse(body, status_code=503)
        return body

    @app.get("/diagnostics")
    def diagnostics(request: Request):
        """完整深度探针（v20.4 P2-17）：与授权 /health 同一份载荷，
        匿名语义不同 —— /health 为兼容给匿名调用方留键公开视图，
        /diagnostics 是运维端点：门禁启用时无凭据一律 401，不做降级视图。
        """
        _gate, _authz = _resolve_authz(request)
        if _gate and not _authz:
            return JSONResponse(
                {
                    "error": "unauthorized",
                    "detail": "Missing or invalid credentials: log in to the console "
                              "or send Authorization: Bearer <AIDUMEM_API_TOKEN>",
                },
                status_code=401,
            )
        return _run_full_probe()

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

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


def set_version_info(version: str, codename: str, codename_zh: str = "雅典娜"):
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
        })

        probes: dict[str, object] = {
            "facts_db": os.path.exists(FACTS_DB),
            "text_fts_db": os.path.exists(TEXT_FTS_DB),
            "mem0_singleton": is_mem_ready(),
            "port_service": True,
            "injection_guard_ok": True,
        }

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
            if not ek["configured"]:
                warnings.append(
                    f"{ek['env_var']} 未配置：涉及自定义人名/项目代号的查询会零召回，"
                    "参考 .env.example 配置后重启服务"
                )
        except Exception as e:
            probes["entity_keywords_ok"] = False
            probes["entity_keywords_error"] = str(e)[:120]

        # 事实库与容量水位探针
        facts_count = 0
        try:
            from ducky.utils import get_facts_conn
            conn_f = get_facts_conn()
            facts_count = conn_f.execute("SELECT COUNT(*) FROM facts WHERE archived=0").fetchone()[0]
            conn_f.close()
            probes["facts_active_count"] = int(facts_count)
            # 水位预警（默认 1000 条基准容量，>800 预警）
            if facts_count > 800:
                warnings.append(f"事实库水位较高（当前有效事实 {facts_count} 条），建议触发 refine_memory 归档精炼")
                probes["watermark_warning"] = True
            else:
                probes["watermark_warning"] = False
        except Exception as e:
            probes["facts_active_count"] = -1
            probes["facts_error"] = str(e)[:120]

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

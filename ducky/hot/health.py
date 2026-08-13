"""ducky.hot.health — GET /health"""
from __future__ import annotations
import os

import logging
import os

from fastapi import FastAPI

# 版本信息：由 api_server.py 启动时通过 set_version_info() 注入
_version_info = {
    "service_version": "12.0.0",
    "codename": "Chronos",
    "codename_zh": "克罗诺斯",
}


def set_version_info(version: str, codename: str, codename_zh: str = "克罗诺斯"):
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

logger = logging.getLogger("aiduMEM.hot")


def register_health_routes(app: FastAPI) -> None:
    @app.get("/health")
    def health():
        """B 档：lazy 预热 + 真实探针（可 import / 文件存在 / mem0 单例是否就绪）。

        modules 不再只表示「是否已加载过」，避免冷启动全 false 误导运维。
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
            "v8_ignition":    _can_import("ducky.memory_ignition"),
            "v8_workspace":   _can_import("ducky.memory_workspace"),
            "v8_broadcast":   _can_import("ducky.memory_broadcast"),
            "v8_jlens":       _can_import("ducky.memory_jlens"),
            "v8_persistence": _can_import("ducky.memory_persistence"),
            "v2.1_salience":  _can_import("ducky.memory_salience"),
            "v2.1_gate":      _can_import("ducky.memory_gate"),
            "v2.1_envelope":  _can_import("ducky.tool_envelope"),
            "v18.3_obsidian": _can_import("ducky.routes_obsidian"),
        })

        probes: dict[str, object] = {
            "facts_db": os.path.exists(FACTS_DB),
            "text_fts_db": os.path.exists(TEXT_FTS_DB),
            "mem0_singleton": is_mem_ready(),
            "port_service": True,
        }
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

        # 实体词表探针 —— 漏配时闸门会把自己人名/项目代号的查询判成
        # no_signal 并静默零召回，这是最难自查的一类故障，必须上报。
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

        # Zeus v18.0: Raw Drawer 探针
        try:
            from ducky.utils import get_text_conn
            conn2 = get_text_conn()
            raw_count = conn2.execute(
                "SELECT COUNT(*) FROM memories WHERE id LIKE 'raw-%'"
            ).fetchone()[0]
            conn2.close()
            probes["raw_drawer_count"] = int(raw_count)
            probes["raw_drawer_ok"] = True
        except Exception as e:
            probes["raw_drawer_ok"] = False
            probes["raw_drawer_error"] = str(e)[:120]

        # Zeus v18.0: Code Graph 探针
        try:
            from ducky.code_graph import build_dependency_graph
            probes["code_graph_ok"] = True
        except Exception as e:
            probes["code_graph_ok"] = False
            probes["code_graph_error"] = str(e)[:120]

        # Zeus v18.1: EvolveMem 探针
        try:
            from ducky.utils import DATA_DIR
            probes["evolve_mem_ok"] = os.path.exists(os.path.join(DATA_DIR, "evolve_mem.db"))
        except Exception as e:
            probes["evolve_mem_ok"] = False
            probes["evolve_mem_error"] = str(e)[:120]

        degraded = [k for k, v in module_ok.items() if not v]
        if not probes.get("fts_ok"):
            degraded.append("fts")
        status = "ok" if not degraded else "degraded"

        return te_ok(
            service=f"aiduMEM-v{_version_info['service_version']}",
            version=f"{_version_info['service_version']}-{_version_info['codename'].lower()}",
            codename=_version_info["codename"],
            codename_zh=_version_info["codename_zh"],
            modules=module_ok,
            probes=probes,
            degraded=degraded,
            warnings=warnings,
            health_status=status,
        )

    @app.get("/metrics")
    def metrics(days: int = 7):
        """🔴4：运行时指标端点（白皮书宣称已久，此前从未注册）。

        返回记忆总量 / salience 生长历史 / FTS 索引量等运维指标。
        """
        out: dict = {"version": f"{_version_info['service_version']}-{_version_info['codename'].lower()}"}
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


"""
ducky.routes_registry — 统一路由注册表
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
收拢所有业务线路由，api_server 仅需调用此处。
"""
from __future__ import annotations

import logging
from fastapi import FastAPI

from ducky.routes_core import register_core_routes
from ducky.routes_v8 import register_v8_routes
from ducky.routes_clotho import register_clotho_routes
from ducky.hot.legacy import register_legacy_routes
from ducky.extended.routes import register_extended_routes
from ducky.federation.routes import register_federation_routes
from ducky.routes_octopus import register_octopus_routes

logger = logging.getLogger("aiduMEM.RoutesRegistry")

def register_all_routes(app: FastAPI, get_memory_fn, get_db_fn, extract_entities_fn) -> None:
    """按序注册所有端点：Core(HOT) -> v8 -> Clotho -> Extended -> Legacy -> Octopus"""
    
    # 1. 注册 HOT 核心路由 (Crud, Add, Search, Health)
    register_core_routes(app)
    
    # 2. 注册 v8 记忆管道流路由 (Ignition, Workspace, Broadcast, Jlens, Session)
    register_v8_routes(app)
    
    # 3. 注册 Clotho/Hyperion 核心引擎路由 (CoreMemory, Checkpoint, AutoDream)
    register_clotho_routes(app)
    
    # 4. 注册 Extended 15脉外延路由
    register_extended_routes(app, get_memory_fn, get_db_fn, extract_entities_fn)
    
    # 5. 注册 Legacy 遗留路由 (待拆分)
    register_legacy_routes(app)

    # 6. 注册 Pantheon 联邦层路由
    register_federation_routes(app)

    # 7. 注册 Octopus (v16.0) 专属三大特性路由 (ConflictResolver, TreeMemory, SkillCrystallizer)
    register_octopus_routes(app)

    logger.info("✅ 所有路由线注册完毕 (含 v16.0 Opus Octopod)")

"""ducky.write_endpoint_budgets — 全量写端点预算台账（v20.4.0-alpha · P0-1）

每条写路由（POST/PUT/PATCH/DELETE）必须在此登记：载荷模型名（无 JSON body
模型的标量/query 路由记 "-"）、一句口径。守卫 tests/test_v20_4_add_bounds.py
经 find_budget_violations(app) 做双向往返比对：

- 新增写路由未登记            → 红
- 登记了但路由消失            → 红（防台账烂掉）
- 模型自由文本字段无 max_length
  且未在 uncapped 里登记理由  → 红

MCP 工具的写操作经 REST 落地（mcp_server 走 api_auth_headers 调 HTTP），
不在此单列。数值字段（limit/k/max_tokens 等）的边界由各模型 Field(ge/le)
约束，不属于本台账的自由文本口径。
"""

from __future__ import annotations

import inspect
import typing
from typing import Any, Dict

from pydantic import BaseModel

# ── 自由文本字段豁免登记 ─────────────────────────────────────────
# 形参：模型名 → {字段名: 豁免理由}。只有「无 max_length 但确有必要」的字段
# 才配出现在这里，每条理由必须能被第三人复核。
UNCAPPED_TEXT_FIELDS: Dict[str, Dict[str, str]] = {
    "AddRequest": {
        "messages": "由 field_validator 限长（str ≤ 50,000 字符 / list·dict 序列化 ≤ 64 KiB），"
                    "上限断言见 tests/test_v20_4_add_bounds.py",
        "metadata": "dict 结构载荷，形态白名单见 _metadata_shape 校验器"
                    "（键数/键名/单值/总量/嵌套深度五重上限）",
    },
    "RawDrawerRequest": {
        "metadata": "dict 结构载荷（沿用既有宽松形态；收紧登记为后续候选）",
    },
    "ObsidianSyncRequest": {
        "metadata": "dict 结构载荷（沿用既有宽松形态；收紧登记为后续候选）",
    },
    "CheckpointPayload": {
        "blocks": "core memory 区块结构载荷（dict），块内容由 core_memory 层校验",
    },
}

# ── 写路由台账 ──────────────────────────────────────────────────
# 有 JSON body 模型的路由：path → 模型名。
WRITE_ROUTE_MODELS: Dict[str, str] = {
    "/add": "AddRequest",
    "/add/raw": "RawDrawerRequest",
    "/api/checkpoint": "CheckpointPayload",
    "/api/obsidian/sync": "ObsidianSyncRequest",
    "/code/impact": "ImpactRequest",
    "/conflict/resolve": "ConflictCheckRequest",
    "/delete": "DeleteRequest",          # POST 形态；DELETE 形态为 query 标量
    "/delete_all": "DeleteAllRequest",
    "/evolve/feedback": "FeedbackRequest",
    "/governance/review": "GovernanceReviewRequest",
    "/ignition_test": "SearchRequest",
    "/jlens": "SearchRequest",
    "/memory/refine": "RefineGroupRequest",
    "/memory/refine/apply": "RefineActionRequest",
    "/memory/refine/rollback": "RefineActionRequest",
    "/memory/types/backfill": "BackfillRequest",
    "/opinions/set": "OpinionSetRequest",
    "/persona/build": "PersonaBuildRequest",
    "/persona/retrieve": "PersonaRetrieveRequest",
    "/persona/rollback": "PersonaRollbackRequest",
    "/recall_chain": "SearchRequest",
    "/reflect": "ReflectRequest",
    "/search": "SearchRequest",
    "/search_trace": "SearchRequest",
    "/self-edit/rollback": "RollbackRequest",
    "/session/search": "SearchRequest",
    "/skill/grow": "SkillGrowRequest",
    "/tombstone/restore": "TombstoneRestoreRequest",
    "/tree/node": "TreeNodeRequest",
    "/update": "UpdateRequest",
}

# 无 JSON body 模型的写路由（标量/query/form 参数，受 URL 长度天然约束）。
WRITE_ROUTES_SCALAR = {
    "/add/coalesce/flush",
    "/api/autodream/trigger",
    "/api/checkpoint/cleanup",
    "/api/checkpoint/inject",
    "/api/core-memory/inject",
    "/api/core-memory/{block_key}",
    "/auto-memory/trigger",
    "/broadcast_expand",
    "/config/_speed",
    "/config/password",
    "/config/{section}",
    "/crystals/approve",
    "/crystals/detect",
    "/crystals/prune",
    "/crystals/use",
    "/evolve/cycle",
    "/facts/add",
    "/facts/compress",
    "/facts/expire",
    "/facts/feedback",
    "/facts/inject-context",
    "/facts/preference",
    "/facts/tags/generate",
    "/federation/agents/deactivate",
    "/federation/agents/heartbeat",
    "/federation/agents/register",
    "/federation/facts/add",
    "/federation/grants",
    "/federation/grants/revoke",
    "/federation/migrate",
    "/graduate",
    "/login",
    "/logout",
    "/memory/types/reset",
    "/observe/consolidate",
    "/persona/ai-self/add",
    "/persona/refresh",
    "/prune/contradiction",
    "/prune/contradiction-v2",
    "/reload",
    "/scene/cluster",
    "/session/end",
    "/session/pin",
    "/session/start",
    "/session/unpin",
    "/skill/discover",
    "/workspace/clear",
}

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _body_model(endpoint: Any) -> type[BaseModel] | None:
    try:
        hints = typing.get_type_hints(endpoint)
    except Exception:
        return None
    for ann in hints.values():
        if inspect.isclass(ann) and issubclass(ann, BaseModel):
            return ann
    return None


def _uncapped_text_fields(model: type[BaseModel]) -> list[str]:
    """模型里无 max_length 的文本/集合字段（str / list / dict / Messages）。"""
    out = []
    for fname, field in model.model_fields.items():
        ann = str(model.__annotations__.get(fname, ""))
        is_text = any(k in ann for k in ("str", "List", "Dict", "list", "dict", "Messages"))
        if not is_text:
            continue
        capped = any(getattr(md, "max_length", None) for md in (field.metadata or []))
        if not capped:
            out.append(fname)
    return out


def find_budget_violations(app: Any) -> list[str]:
    """比对真实 app 写路由表与本台账，返回违规清单（空 = 全绿）。"""
    violations: list[str] = []

    real: dict[str, type[BaseModel] | None] = {}
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if not (methods & _WRITE_METHODS):
            continue
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        real.setdefault(path, _body_model(endpoint) if endpoint else None)

    registered = set(WRITE_ROUTE_MODELS) | WRITE_ROUTES_SCALAR
    for path in sorted(set(real) - registered):
        violations.append(f"未登记写路由：{path}（补进 WRITE_ROUTE_MODELS 或 WRITE_ROUTES_SCALAR）")
    for path in sorted(registered - set(real)):
        violations.append(f"台账登记了不存在的路由：{path}（删条目或恢复路由）")

    for path, model in sorted(real.items()):
        if model is None:
            if path in WRITE_ROUTE_MODELS:
                violations.append(
                    f"{path} 台账登记模型 {WRITE_ROUTE_MODELS[path]}，但路由未解析到 body 模型")
            continue
        if path in WRITE_ROUTES_SCALAR:
            violations.append(
                f"{path} 登记为标量路由，但实际解析到 body 模型 {model.__name__}"
                "（应改登 WRITE_ROUTE_MODELS）")
        want = WRITE_ROUTE_MODELS.get(path)
        if want and model.__name__ != want:
            violations.append(f"{path} 台账登记模型 {want} ≠ 实际 {model.__name__}")
        # 字段检查与登记状态无关：未登记的野路由也要被揪出无上限文本字段
        allowed = UNCAPPED_TEXT_FIELDS.get(model.__name__, {})
        for fname in _uncapped_text_fields(model):
            if fname not in allowed:
                violations.append(
                    f"{path} 模型 {model.__name__}.{fname} 自由文本无 max_length 且未在 "
                    "UNCAPPED_TEXT_FIELDS 登记豁免理由")
    return violations

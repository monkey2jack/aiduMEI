"""ducky.hot.legacy — Legacy 模块向后兼容入口门面（v19.3.0 拆分解耦）"""
from __future__ import annotations

from ducky.hot.legacy_helpers import (
    _get_db,
    _get_facts_conn,
    _get_obs_conn,
    _get_scenes_conn,
    _extract_entities,
    _extract_key_facts,
    _auto_extract_and_link,
    _cluster_scenes_impl,
    _extract_validity,
    TAGS_FILE,
    SKILL_PATTERNS_FILE,
)
from ducky.hot.legacy_routes import register_legacy_routes

__all__ = [
    "_get_db",
    "_get_facts_conn",
    "_get_obs_conn",
    "_get_scenes_conn",
    "_extract_entities",
    "_extract_key_facts",
    "_auto_extract_and_link",
    "_cluster_scenes_impl",
    "_extract_validity",
    "TAGS_FILE",
    "SKILL_PATTERNS_FILE",
    "register_legacy_routes",
]

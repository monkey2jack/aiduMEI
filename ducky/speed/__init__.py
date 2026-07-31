"""
ducky.speed — Mnemosyne 高速写入子包（v9.1）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
拆自 add_speed.py。对外经 ducky.add_speed 兼容 re-export。
"""
from ducky.speed.cache import cache_get, cache_key, cache_set
from ducky.speed.coalesce import (
    coalesce_enqueue,
    coalesce_flush_due,
    coalesce_note,
    coalesce_should_buffer,
    coalesce_status,
    ensure_coalesce_worker,
    register_coalesce_flusher,
    resolve_coalesce_profile,
)
from ducky.speed.config import load_speed_cfg, messages_to_text
from ducky.speed.fastpath import try_fastpath_text
from ducky.speed.jobs import job_create, job_get, job_update
from ducky.speed.patch import patch_llm_for_speed
from ducky.speed.pipeline import run_add_pipeline
from ducky.speed.stats import (
    coalesce_stats_snapshot,
    record_coalesce_enqueue,
    record_coalesce_wave,
)

__all__ = [
    "load_speed_cfg",
    "messages_to_text",
    "cache_key",
    "cache_get",
    "cache_set",
    "try_fastpath_text",
    "job_create",
    "job_update",
    "job_get",
    "record_coalesce_enqueue",
    "record_coalesce_wave",
    "coalesce_stats_snapshot",
    "resolve_coalesce_profile",
    "coalesce_should_buffer",
    "coalesce_enqueue",
    "coalesce_flush_due",
    "coalesce_status",
    "register_coalesce_flusher",
    "ensure_coalesce_worker",
    "coalesce_note",
    "patch_llm_for_speed",
    "run_add_pipeline",
]

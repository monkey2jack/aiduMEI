"""aiduMEM 配置只读路由：GET /config · GET/POST /config/_speed

供 aiduMEI 控制台 SETTINGS 面板读取模型配置与可调参数。
api_key 始终脱敏返回；_speed 参数支持在线微调（写入 mem0_config_local.json）。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional

from fastapi import FastAPI

from ducky.speed.config import _CFG_PATH, load_speed_cfg

_WRITE_LOCK = threading.Lock()


def _mask_key(key: Optional[str]) -> str:
    if not key:
        return "—"
    if len(key) <= 8:
        return key[:1] + "***"
    return key[:3] + "***" + key[-4:]


def _load_raw_config() -> dict:
    try:
        with open(_CFG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _build_config_view() -> dict:
    raw = _load_raw_config()
    llm = raw.get("llm") or {}
    emb = raw.get("embedder") or {}
    rer = raw.get("rerank") or {}
    vs = raw.get("vector_store") or {}
    lc = llm.get("config") or {}
    ec = emb.get("config") or {}
    rc = rer.get("config") or {} if isinstance(rer, dict) else {}
    vsc = vs.get("config") or {} if isinstance(vs, dict) else {}
    return {
        "llm": {
            "provider": llm.get("provider"),
            "config": {
                "model": lc.get("model"),
                "openai_base_url": lc.get("openai_base_url"),
                "api_key": _mask_key(lc.get("api_key")),
                "max_tokens": lc.get("max_tokens"),
                "temperature": lc.get("temperature"),
                "is_reasoning_model": lc.get("is_reasoning_model", False),
                "reasoning_effort": lc.get("reasoning_effort", "none"),
                "_note": lc.get("_note", ""),
            },
        },
        "embedder": {
            "provider": emb.get("provider"),
            "config": {
                "model": ec.get("model"),
                "openai_base_url": ec.get("openai_base_url"),
                "api_key": _mask_key(ec.get("api_key")),
            },
        },
        "rerank": {
            "enabled": bool(rer.get("enabled")) if isinstance(rer, dict) else False,
            "provider": rer.get("provider") if isinstance(rer, dict) else None,
            "config": {
                "model": rc.get("model"),
                "openai_base_url": rc.get("openai_base_url"),
                "api_key": _mask_key(rc.get("api_key")),
            },
        },
        "vector_store": {
            "provider": vs.get("provider") if isinstance(vs, dict) else None,
            "config": {
                "collection_name": vsc.get("collection_name"),
                "embedding_model_dims": vsc.get("embedding_model_dims"),
                "path": vsc.get("path"),
            },
        },
        "_speed": load_speed_cfg(),
        "readonly": os.environ.get("AIDUMEM_CONFIG_READONLY", "0").lower()
        in {"1", "true", "yes"},
        "path": _CFG_PATH,
    }


def register_config_routes(app: FastAPI) -> None:
    @app.get("/config")
    def get_config() -> dict:
        return _build_config_view()

    @app.get("/config/_speed")
    def get_speed() -> dict:
        return load_speed_cfg()

    @app.post("/config/_speed")
    def update_speed(body: dict) -> dict:
        """在线微调 _speed 参数。body: {key, value} 或 {updates: {k:v,...}}。"""
        if os.environ.get("AIDUMEM_CONFIG_READONLY", "0").lower() in {"1", "true", "yes"}:
            return {"status": "error", "detail": "当前为只读演示模式：配置不可在线修改"}
        key = body.get("key")
        value = body.get("value")
        updates = body.get("updates") or ({key: value} if key else {})
        if not updates:
            return {"status": "error", "detail": "未提供要更新的参数"}
        with _WRITE_LOCK:
            raw = _load_raw_config()
            speed_section = dict(raw.get("_speed") or {})
            for k, v in updates.items():
                speed_section[k] = v
            raw["_speed"] = speed_section
            # 原子写入：先写临时文件再 rename，避免中途崩溃损坏配置
            cfg_dir = os.path.dirname(_CFG_PATH)
            fd, tmp_path = tempfile.mkstemp(dir=cfg_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, _CFG_PATH)
            except Exception:
                os.unlink(tmp_path)
                raise
        return {"status": "ok", "updated": updates, "_speed": load_speed_cfg()}

"""aiduMEM 配置只读路由：GET /config · GET/POST /config/_speed

供 aiduMEI 控制台 SETTINGS 面板读取模型配置与可调参数。
api_key 始终脱敏返回；_speed 参数支持在线微调（写入 mem0_config_local.json）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ducky.speed.config import _CFG_PATH, load_speed_cfg

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()

# 允许通过 UI 在线编辑的配置段
_PUT_SECTIONS = {"llm", "embedder", "rerank", "vector_store", "vision", "_features"}


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
    vis = raw.get("vision") or {}
    vs = raw.get("vector_store") or {}
    lc = llm.get("config") or {}
    ec = emb.get("config") or {}
    rc = rer.get("config") or {} if isinstance(rer, dict) else {}
    vc = vis.get("config") or {} if isinstance(vis, dict) else {}
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
        "vision": {
            "provider": vis.get("provider") or llm.get("provider"),
            "config": {
                "model": vc.get("model") or lc.get("model"),
                "openai_base_url": vc.get("openai_base_url") or lc.get("openai_base_url"),
                "api_key": _mask_key(vc.get("api_key") if vc.get("api_key") else lc.get("api_key")),
                "max_tokens": vc.get("max_tokens"),
            },
        },
        "features": raw.get("_features", {
            "obsidian": True,
            "vision": True,
            "fast_update": True
        }),
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

    @app.put("/config/{section}")
    def update_config(section: str, body: dict) -> dict:
        """UI 保存模型配置：PUT /config/llm|embedder|rerank|vector_store。

        body 与 GET /config 同构（provider + config）。合并语义：
        api_key 传空视为不修改；rerank 未显式给 enabled 时按是否填了
        model/base_url 自动判断。写回 mem0_config_local.json 后热生效。
        """
        if os.environ.get("AIDUMEM_CONFIG_READONLY", "0").lower() in {"1", "true", "yes"}:
            return JSONResponse(
                {"status": "error", "detail": "当前为只读演示模式：配置不可在线修改"},
                status_code=403,
            )
        if section not in _PUT_SECTIONS:
            return JSONResponse(
                {"status": "error", "detail": f"不支持的配置段: {section}"},
                status_code=400,
            )
        with _WRITE_LOCK:
            raw = _load_raw_config()
            old_section = dict(raw.get(section) or {})
            old_cfg = dict(old_section.get("config") or {})
            new_cfg = dict((body.get("config") or {}))
            for k, v in new_cfg.items():
                if k == "api_key" and (v is None or str(v).strip() == ""):
                    continue
                old_cfg[k] = v
            new_provider = body.get("provider") or old_section.get("provider")
            if section == "rerank":
                if "enabled" in body:
                    enabled = bool(body.get("enabled"))
                else:
                    enabled = bool(old_cfg.get("model") or old_cfg.get("openai_base_url"))
                raw[section] = {"enabled": enabled, "provider": new_provider, "config": old_cfg}
            elif section == "_features" or section == "features":
                # 模块开关：直接合并布尔值
                old_features = dict(raw.get("_features") or {})
                for k, v in new_cfg.items():
                    old_features[k] = bool(v)
                raw["_features"] = old_features
            else:
                old_section["provider"] = new_provider
                old_section["config"] = old_cfg
                raw[section] = old_section
            cfg_dir = os.path.dirname(_CFG_PATH)
            fd, tmp_path = tempfile.mkstemp(dir=cfg_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, _CFG_PATH)
            except Exception:
                os.unlink(tmp_path)
                raise
        logger.info("🛠️ 配置段已在线更新: %s", section)
        return {"status": "ok", "updated": section, "config": _build_config_view()}

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

    # ═══════════════════════════════════════════════════════════════════
    # 修改登录密码（v18.3）
    # ═══════════════════════════════════════════════════════════════════
    @app.post("/config/password")
    def change_password(body: dict) -> dict:
        """修改 UI 登录密码。需要验证当前密码，成功后写入 .env 文件并提示重启。"""
        import hmac

        current = body.get("current_password", "")
        new = body.get("new_password", "")
        confirm = body.get("confirm_password", "")

        # 验证当前密码
        current_stored = os.environ.get("AIDUMEM_UI_PASSWORD") or "123456"
        if not isinstance(current, str) or not hmac.compare_digest(current, current_stored):
            return {"status": "error", "detail": "当前密码错误 / Current password incorrect"}

        # 验证新密码
        if not isinstance(new, str) or len(new) < 4:
            return {"status": "error", "detail": "新密码至少 4 位 / New password too short"}
        if new != confirm:
            return {"status": "error", "detail": "两次输入不一致 / Passwords do not match"}

        # 写入 .env 文件（追加或更新 AIDUMEM_UI_PASSWORD）
        env_path = os.path.join(os.path.dirname(_CFG_PATH), ".env")
        env_lines = []
        key_found = False

        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("AIDUMEM_UI_PASSWORD="):
                        env_lines.append(f"AIDUMEM_UI_PASSWORD={new}\n")
                        key_found = True
                    else:
                        env_lines.append(line)

        if not key_found:
            env_lines.append(f"AIDUMEM_UI_PASSWORD={new}\n")

        try:
            with open(env_path, "w") as f:
                f.writelines(env_lines)
            logger.info("🔐 UI 登录密码已更新（需重启服务生效）")
            return {
                "status": "ok",
                "detail": "密码已更新，重启服务后生效 / Password updated, restart to take effect",
                "restart_required": True
            }
        except Exception as e:
            logger.error(f"密码写入失败: {e}")
            return {"status": "error", "detail": f"写入失败: {e}"}

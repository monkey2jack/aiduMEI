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


class ConfigUnreadable(RuntimeError):
    """配置文件**在**但读不出来 —— 这不是「空配置」，是部署坏了。

    v20.3.2 正式版（外审 Codex F-05）：原 `_load_raw_config` 任何异常一律 `return {}`，
    随后 PUT 基于这个空字典写临时文件并 `os.replace`。原子写只保证「不出现半个文件」，
    **不保证写的内容基于正确的旧状态** —— 一次瞬时 I/O 抖动 + 一次保存，其余全部配置段
    就没了。读路径可以宽（GET 拿不到就显示空），**写路径必须严**。
    """


def _load_raw_config() -> dict:
    """读路径：拿不到就当空（供 GET / 展示用）。写路径不许用这个。"""
    try:
        with open(_CFG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_raw_config_for_write() -> dict:
    """写路径：**只有「文件不存在」才允许初始化为空**；损坏/无权限/IO 一律抛。"""
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise ConfigUnreadable(
            f"配置文件存在但无法读取（{type(exc).__name__}: {str(exc)[:80]}）；"
            "拒绝在其上覆盖写入，原文件保持不变。请先修复或手动备份后删除。"
        ) from exc


def _atomic_write_config(raw: dict) -> None:
    """写前留 .bak，临时文件 fsync 后 replace —— 掉电只见旧版或完整新版。"""
    cfg_dir = os.path.dirname(_CFG_PATH) or "."
    if os.path.exists(_CFG_PATH):
        try:
            import shutil
            shutil.copy2(_CFG_PATH, _CFG_PATH + ".bak")
        except Exception as exc:  # 备份失败不阻塞写入，但必须出声
            logger.warning("配置备份 .bak 失败（继续写入）: %s", exc)
    fd, tmp_path = tempfile.mkstemp(dir=cfg_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _CFG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


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
                # v20 · P1-4：不许兜底成 "none"。配置里没写就是 None ——
                # 兜一个字符串出来，控制台会显示得像「这个旋钮已经设好了」，
                # 而实际上配置里一个字都没有。「配置没写却显示写了」和
                # 「配置写了不等于配置生效」是同一种假绿灯。
                "reasoning_effort": lc.get("reasoning_effort"),
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
            try:
                raw = _load_raw_config_for_write()
            except ConfigUnreadable as exc:
                return JSONResponse({"status": "error", "code": "config_unreadable",
                                     "detail": str(exc)}, status_code=409)
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
            _atomic_write_config(raw)
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
            try:
                raw = _load_raw_config_for_write()
            except ConfigUnreadable as exc:
                return JSONResponse({"status": "error", "code": "config_unreadable",
                                     "detail": str(exc)}, status_code=409)
            speed_section = dict(raw.get("_speed") or {})
            for k, v in updates.items():
                speed_section[k] = v
            raw["_speed"] = speed_section
            # 原子写入：先写临时文件再 rename，避免中途崩溃损坏配置
            _atomic_write_config(raw)
        return {"status": "ok", "updated": updates, "_speed": load_speed_cfg()}

    # ═══════════════════════════════════════════════════════════════════
    # 修改登录密码（v18.3）
    # ═══════════════════════════════════════════════════════════════════
    @app.post("/config/password")
    def change_password(body: dict) -> dict:
        """修改 UI 登录口令。

        v19.4.1（P2-2 / P0-1）：
          · 哈希统一走 ducky.security.auth（PBKDF2-HMAC-SHA256 200k 轮），
            旧格式 `salt:sha256hex` 校验通过后自动升级，存量部署无感；
          · 改密成功后**撤销全部既有会话**，强制所有端重新登录 ——
            否则老会话仍能用旧凭据继续访问，改密等于没改。
        """
        from ducky.security.auth import (
            check_ui_password,
            hash_password,
            revoke_all_sessions,
            write_password_hash,
        )

        current = body.get("current_password", "")
        new = body.get("new_password", "")
        confirm = body.get("confirm_password", "")

        if not check_ui_password(current if isinstance(current, str) else ""):
            return {"status": "error", "detail": "当前密码错误 / Current password incorrect"}

        if not isinstance(new, str) or len(new) < 8:
            # v19.4.1：下限从 4 位提到 8 位。控制台口令是记忆库的唯一门禁，
            # 4 位口令在本地爆破面前形同虚设。
            return {"status": "error", "detail": "新密码至少 8 位 / New password too short (min 8)"}
        if new != confirm:
            return {"status": "error", "detail": "两次输入不一致 / Passwords do not match"}

        if not write_password_hash(hash_password(new)):
            return {"status": "error", "detail": "更新失败：无法写入口令哈希文件"}

        # 🔴v20.3.2-beta（外审 M-2）：**不再把明文口令写进 os.environ。**
        # 上一行 write_password_hash() 已经落盘哈希，门禁判据读的就是那个文件，
        # 所以这一行对功能毫无贡献；而它的代价是明文常驻 /proc/<pid>/environ，
        # 并被**所有子进程**（mem0、ffmpeg…）继承。既多余又有害，直接删。
        # （auth.py 的 docstring 早写过同一条道理：口令的读者范围只许缩小。）
        revoked = revoke_all_sessions()
        logger.info("🔐 UI 登录口令已更新（PBKDF2 哈希），已撤销 %d 个既有会话", revoked)
        return {
            "status": "ok",
            "detail": "密码已更新并即时生效，所有已登录会话已失效 / "
                      "Password updated; all existing sessions revoked",
            "sessions_revoked": revoked,
            "restart_required": False,
        }

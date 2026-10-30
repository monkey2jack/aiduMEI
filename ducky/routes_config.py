"""aiduMEM 配置只读路由：GET /config · GET/POST /config/_speed

供 aiduMEI 控制台 SETTINGS 面板读取模型配置与可调参数。
api_key 始终脱敏返回；_speed 参数支持在线微调（写入 mem0_config_local.json）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from ducky.speed.config import _CFG_PATH, load_speed_cfg
from ducky.utils import DEFAULT_AGENT_ID, DEFAULT_USER_ID

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()

# 允许通过 UI 在线编辑的配置段
_PUT_SECTIONS = {"llm", "embedder", "rerank", "decision", "vector_store", "vision", "_features"}

# v20.4.0（三方审计 P2-1 · Codex P1-10）：**字段级白名单**。原实现 body 的
# config 字段全盘合并 —— 持普通 API 凭据的调用方可以写入任意键（含未来
# 代码会消费的键），配置面成了权限升级面。每段只收它语义内的键；
# base_url 只许 http(s)（挡 file:// 之类的协议走私），路径类键只许落在
# 数据根内。全禁在线写走 AIDUMEM_CONFIG_READONLY=1（既有只读阀）。
_SECTION_FIELD_ALLOWLIST: dict[str, set] = {
    "llm": {"model", "openai_base_url", "api_key", "temperature", "max_tokens",
            "top_p", "is_reasoning_model", "_note"},
    "vision": {"model", "openai_base_url", "api_key", "_note"},
    "embedder": {"model", "openai_base_url", "api_key", "embedding_dims", "_note"},
    "rerank": {"model", "openai_base_url", "api_key", "top_n", "_note"},
    "decision": {"model", "openai_base_url", "api_key", "account_id", "tasks", "users", "timeout_ms", "threshold", "mode", "_note"},
    "vector_store": {"collection_name", "path", "host", "port", "embedding_model_dims", "_note"},
}
_URL_FIELDS = {"openai_base_url", "host"}

# f0.6 W-12：外呼四段（decision 段走自己的 validate，不在此列）
_EGRESS_SECTIONS = {"llm", "embedder", "rerank", "vision"}

def _host_as_ip(host: str):
    """主机名若是 IP 的任一写法就返回 ip_address，否则 None（域名）。

    f0.7 P1-4（外审 muse P1-06）：ipaddress 只认点分十进制，而 HTTP 客户端经 inet_aton 也认
    十进制整数（2130706433）、十六进制（0x7f000001 / 0x7f.0.0.1）、八进制（0177.0.0.1）——
    这些写法此前被当成「域名」放行，私网拦截形同虚设。"""
    import ipaddress
    import socket
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if re.fullmatch(r"[0-9a-fx.]+", h) and any(c.isdigit() for c in h):
        try:
            return ipaddress.ip_address(socket.inet_aton(h))
        except OSError:
            return None
    return None


def _model_endpoint_allow() -> set:
    raw = os.environ.get("AIDUMEI_MODEL_ENDPOINT_ALLOW", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _reject_private_endpoint(url: str) -> str | None:
    """拦截私网/链路本地/元数据地址与明文 http（muse S-1：169.254.169.254
    与内网端点此前全放行，是把凭据+记忆原文转投内网/云元数据的通道）。"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return "无法解析主机名"
        # 元数据/内部地址最先拦——它们以 https 出现也不放行
        if host == "169.254.169.254" or host.endswith(".internal"):
            return "云元数据/内部地址禁止作为模型端点"
        # f0.7：自家内网/tailnet 上的 vLLM 等模型机——部署方在 .env 显式点名才放行
        # (AIDUMEI_MODEL_ENDPOINT_ALLOW=gpu-box,192.168.1.50,100.64.0.20)。
        # 元数据地址已在上面先拦,名单也放不过。
        if host in _model_endpoint_allow():
            return None
        if parsed.scheme == "http" and host not in ("127.0.0.1", "localhost"):
            return "明文 http 仅允许 loopback（127.0.0.1/localhost）；远程端点必须 https"
        ip = _host_as_ip(host)
        if ip is not None:
            import ipaddress
            _loopback_ok = ip.is_loopback and host in ("127.0.0.1",)
            # f0.7 P1-4（外审 muse P1-06）：补 CGNAT 100.64/10（tailnet 地址段）与 IPv4 映射的 IPv6
            _cgnat = ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")
            if ip.version == 6 and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            if not _loopback_ok and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                                     or ip.is_unspecified or ip.is_multicast or _cgnat):
                return f"私网/保留地址禁止作为模型端点: {host}"
        return None
    except Exception as exc:
        return f"端点校验失败: {exc}"


def _validate_config_fields(section: str, cfg: dict, body: dict | None = None, old_section: dict | None = None) -> str | None:
    """返回错误文案；None = 通过。"""
    if section == "decision":
        return _validate_decision_section(cfg, body or {}, old_section or {})
    if section in {"rerank", "decision"} and body is not None and "enabled" in body:
        if not isinstance(body["enabled"], bool):
            return f"{section}.enabled must be a JSON boolean"
    if section == "rerank" and body is not None and body.get("provider") is not None:
        from ducky.mem0_runtime import RERANK_PROVIDERS
        if not isinstance(body["provider"], str) or body["provider"].lower() not in RERANK_PROVIDERS:
            return "不支持的 rerank 供应商: " + str(body.get("provider"))[:40]
    allow = _SECTION_FIELD_ALLOWLIST.get(section)
    if allow is None:
        return None  # _features 走布尔合并分支，不进这里
    unknown = sorted(set(cfg) - allow)
    if unknown:
        return f"配置段 {section} 不接受字段: {', '.join(unknown)}（字段级白名单，v20.4.0）"
    for k in _URL_FIELDS & set(cfg):
        v = str(cfg.get(k) or "").strip()
        if v and not (v.startswith("http://") or v.startswith("https://")):
            return f"{section}.{k} 只接受 http(s) URL，收到: {v[:40]}"
    if section in _EGRESS_SECTIONS:
        err = _validate_egress_endpoint(section, cfg, old_section or {})
        if err:
            return err
    if section == "vector_store":
        return _validate_vector_store_path(cfg)
    return None


def _validate_decision_section(cfg: dict, body: dict, old_section: dict) -> str | None:
    err = _validate_decision_fields(cfg, body, old_section)
    if err:
        return err
    # f0.7 P1-4（外审 muse P1-06）：decision 段同样拦私网/元数据端点（此前只有四段外呼有）
    ep = str(cfg.get("openai_base_url") or "").strip()
    old_ep = str((old_section.get("config") or {}).get("openai_base_url") or "")
    if ep and ep != old_ep:
        _err = _reject_private_endpoint(ep)
        if _err:
            return f"decision.openai_base_url: {_err}"
    return None


def _validate_egress_endpoint(section: str, cfg: dict, old_section: dict) -> str | None:
    """f0.6 W-12（muse S-1 修复项）：decision 段的「换端点必须显式给新 key」
    与私网/元数据地址拦截，推广到 llm / embedder / rerank / vision 四段
    ——此前只有 decision 有此防护，而 S-1 整包转投链走的正是 llm 段。"""
    old_cfg = dict(old_section.get("config") or {})
    ep = str(cfg.get("openai_base_url") or "").strip()
    if not ep or ep == old_cfg.get("openai_base_url"):
        return None
    if old_cfg.get("api_key") and (not isinstance(cfg.get("api_key"), str) or not cfg["api_key"].strip()):
        return (f"changing {section} endpoint requires an explicit API key"
                "（换端点必须显式给新 key，空 key 不再默认沿用旧凭据）")
    _err = _reject_private_endpoint(ep)
    return f"{section}.openai_base_url: {_err}" if _err else None


def _validate_vector_store_path(cfg: dict) -> str | None:
    path_v = str(cfg.get("path") or "").strip()
    if not path_v:
        return None
    import os as _os
    from ducky.utils import DATA_DIR
    real = _os.path.realpath(path_v if _os.path.isabs(path_v) else _os.path.join(DATA_DIR, path_v))
    root = _os.path.realpath(DATA_DIR)
    if real != root and not real.startswith(root + _os.sep):
        return f"vector_store.path 必须位于数据根内（{root}），收到: {path_v[:60]}"
    return None


def _validate_decision_fields(cfg: dict, body: dict, old: dict) -> str | None:
    from ducky.decision import DEFAULTS, validate
    if "provider" in body and not isinstance(body["provider"], str):
        return "decision.provider must be a string"
    if "provider" in body and body["provider"] not in DEFAULTS:
        return "unsupported decision provider"
    if set(body) - {"enabled", "provider", "config"}:
        return "unknown decision section field"
    candidate = _channel_section("decision", body, old, {**(old.get("config") or {}), **cfg})
    old_cfg = old.get("config") or {}
    provider_changed = candidate["provider"] != old.get("provider", "nace")
    endpoint_changed = ("openai_base_url" in cfg
                        and cfg["openai_base_url"] != old_cfg.get("openai_base_url"))
    if provider_changed or endpoint_changed:
        if os.getenv("AIDUMEI_DECISION_API_KEY"):
            return "remove the decision environment key override before changing provider or endpoint"
        if old_cfg.get("api_key") and (not isinstance(cfg.get("api_key"), str) or not cfg["api_key"].strip()):
            return "changing decision provider or endpoint requires an explicit API key"
    if provider_changed and not {"model", "openai_base_url"} <= set(cfg):
        return "changing decision provider requires explicit model and endpoint"
    if provider_changed and candidate["provider"] == "cloudflare" and "account_id" not in cfg:
        return "changing decision provider to cloudflare requires explicit account_id"
    return validate(candidate)


def _decision_defaults(provider: str) -> dict:
    try:
        from ducky.decision import DEFAULTS
        m, u = DEFAULTS.get(provider) or (None, None)
        return {"model": m, "openai_base_url": u}
    except ImportError:
        return {}


def _channel_section(section: str, body: dict, old: dict, cfg: dict) -> dict:
    return {"enabled": body.get("enabled", old.get("enabled", section == "rerank")),
            "provider": (body.get("provider") or old.get("provider")
                         or ("nace" if section == "decision" else "openai_compatible")),
            "config": cfg}


def _require_config_admin(caller: str, operation: str = "config:read") -> None:
    """f0.6 W-11/W-14：配置面统一 admin 判据。

    - UI session（owner 直连）→ 直通（cue A10 的断路在此修通：控制台
      保存配置/改密不再被旧自报判据挡 403）；
    - Bearer → caller 绑定校验 + AIDUMEI_FEDERATION_ADMINS 名单，
      自报字符串不再单独作数；
    - 无凭据回环（门禁关）→ 沿 require_instance_admin 既有放行语义。
    权限失败统一 HTTP 403（此前 /config/_speed 返回 200 包 error 的
    假成功一并纠正）。
    """
    from ducky.scope_auth import require_instance_admin
    try:
        require_instance_admin(caller)
    except HTTPException as exc:
        # 保持与既有 403 细节同构，但带上操作名便于排障
        raise HTTPException(
            status_code=403,
            detail={"error": "config_admin_required", "operation": operation,
                    "detail": str(getattr(exc, "detail", exc))},
        )


def _mask_key(key: Optional[str]) -> str:
    if not key:
        return "—"
    # f0.6 W-11（muse S-1 修复项）：掩码从「首3+尾4共7字符」收紧到「尾2」
    # ——首字符对 sk- 形 key 是固定前缀零信息，尾4配合长度可辅助字典缩小。
    if len(key) <= 8:
        return "***"
    return "***" + key[-2:]


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


def _config_section(raw: dict, section: str) -> dict:
    value = raw.get(section)
    if section == "rerank" and not value:
        value = raw.get("reranker")
    return dict(value or {})


def _build_config_view() -> dict:
    raw = _load_raw_config()
    llm = raw.get("llm") or {}
    emb = raw.get("embedder") or {}
    rer = raw.get("rerank") or raw.get("reranker") or {}
    vis = raw.get("vision") or {}
    vs = raw.get("vector_store") or {}
    lc = llm.get("config") or {}
    ec = emb.get("config") or {}
    rc = rer.get("config") or {} if isinstance(rer, dict) else {}
    dec = raw.get("decision") or {}
    dc = dec.get("config") or {} if isinstance(dec, dict) else {}
    vc = vis.get("config") or {} if isinstance(vis, dict) else {}
    vsc = vs.get("config") or {} if isinstance(vs, dict) else {}
    return {
        # The console must use the server's configured identity when it calls
        # identity-protected federation endpoints.  Never make the browser
        # guess this from a profile or a hard-coded demo value.
        #
        # f0.6 W-11（muse S-1）：agent_id 仍回显（控制台调联邦端点要用，
        # 注释即其存在理由），但 /config 现已与写面同级鉴权（W-11 改造），
        # 「无凭据读配置→拿 admin 名冒充」的链路在读取这一步就断了。
        "agent_id": DEFAULT_AGENT_ID,
        "user_id": DEFAULT_USER_ID,
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
            "enabled": rer.get("enabled", True) is True if isinstance(rer, dict) else False,
            "provider": rer.get("provider") if isinstance(rer, dict) else None,
            "config": {
                "model": rc.get("model"),
                "openai_base_url": rc.get("openai_base_url"),
                "api_key": _mask_key(rc.get("api_key")),
            },
        },
        "decision": {
            "enabled": dec.get("enabled", False) is True,
            "provider": dec.get("provider"),
            "config": {
                "model": dc.get("model"),
                "openai_base_url": dc.get("openai_base_url"),
                "account_id": dc.get("account_id"),
                "api_key": _mask_key(dc.get("api_key")),
                "tasks": dc.get("tasks", {"memory_type": True, "retrieval": True}),
                "users": dc.get("users", []),
                "timeout_ms": dc.get("timeout_ms", 2000),
                "threshold": dc.get("threshold", 0.6),
                "mode": dc.get("mode", "auto"),
                "_note": dc.get("_note", ""),
            },
            # f0.7：没写 model/地址时实际走供应商默认值——单列出来给控制台标「默认」，
            # 不冒充成已填（同 P1-4 口径）。
            "defaults": _decision_defaults(dec.get("provider") or "nace"),
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
    @app.get("/domains")
    def list_all_domains(limit: int = Query(default=100, ge=1, le=500)) -> dict:
        """列出可供控制台选择的**真实活动记忆域**。

        The identity of a domain is the storage pair ``(user_id, bank_id)``.
        Federation ``profile`` is a presentation/grouping attribute, not a
        tenant key.  In particular, this endpoint deliberately has no
        synthetic ``all`` entry: a UI "all domains" mode would silently drop
        the scope parameters on data requests and turn into the service's
        default domain.
        """
        from ducky.bank_contract import DEFAULT_BANK_ID, ensure_memory_banks_schema
        from ducky.utils import get_facts_conn
        conn = get_facts_conn()
        try:
            ensure_memory_banks_schema(conn)
            rows = conn.execute(
                "SELECT user_id, bank_id, display_name, status FROM memory_banks "
                "WHERE status='active' ORDER BY user_id, bank_id LIMIT ?", (limit,)
            ).fetchall()
            domains = []
            for row in rows:
                item = dict(row)
                item["label"] = item.get("display_name") or (
                    f"{item['user_id']} / {item['bank_id']}"
                )
                domains.append(item)
            return {
                "status": "ok",
                "domains": domains,
                "default_domain": {
                    "user_id": DEFAULT_USER_ID,
                    "bank_id": DEFAULT_BANK_ID,
                },
                "limit": limit,
            }
        finally:
            conn.close()

    @app.get("/config")
    def get_config(caller: str = "") -> dict:
        # f0.6 W-11（muse S-1 / wabi F-01）：配置读面与写面同级鉴权——此前
        # 无任何校验即回显 agent_id 与配置绝对路径，攻击者先读后冒充。
        _require_config_admin(caller)
        return _build_config_view()

    @app.put("/config/{section}")
    def update_config(section: str, body: dict, caller: str = "") -> dict:
        """UI 保存模型配置：PUT /config/llm|embedder|rerank|vector_store。

        body 与 GET /config 同构（provider + config）。合并语义：
        api_key 传空视为不修改；rerank 未显式给 enabled 时保留原开关，
        旧配置缺少开关时保持自动。rerank 保存后在下一次调用读取生效。

        v22.0（雷霆审计 B7）：配置面是权限升级面——任何持普通 API 凭据者
        都能改模型配置/口令。加 admin 校验：caller 须在 AIDUMEI_FEDERATION_ADMINS
        名单内，否则 403。
        """
        # f0.6 W-11/W-14：统一 admin 判据——session=owner 直通，Bearer 须
        # 绑定+名单（require_instance_admin），不再用「自报字符串在名单内」
        # 单独作数（cue A10：这条旧判据同时把 UI session 挡在门外）。
        _require_config_admin(caller, operation="config:write")
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
            old_section = _config_section(raw, section)
            old_cfg = dict(old_section.get("config") or {})
            new_cfg = dict((body.get("config") or {}))
            _field_err = _validate_config_fields(section, new_cfg, body, old_section)
            if _field_err:
                return JSONResponse({"status": "error", "detail": _field_err},
                                    status_code=400)
            for k, v in new_cfg.items():
                if k == "api_key" and (v is None or str(v).strip() == ""):
                    continue
                old_cfg[k] = v
            new_provider = body.get("provider") or old_section.get("provider")
            if section in {"rerank", "decision"}:
                raw[section] = _channel_section(section, body, old_section, old_cfg)
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
        # 认知 LLM（治理/蒸馏/reflect 的 call_llm）读的是进程级缓存：写完就失效，
        # 不依赖客户端记得再打 /reload —— 只走 API 改配置的调用方此前会一直用旧
        # endpoint/key（换 key 往往正是因为旧 key 外泄）。mem0 单例仍由 /reload 换代。
        from ducky.llm_client import invalidate_llm_config_cache
        invalidate_llm_config_cache()
        logger.info("🛠️ 配置段已在线更新: %s", section)
        return {"status": "ok", "updated": section, "config": _build_config_view()}


    @app.post("/config/test")
    def test_config(body: dict | None = None, caller: str = "") -> dict:
        """设置页「测试连接」:对各模型段做一次最小真实调用(f0.7)。

        body 可选 {"sections": ["llm", ...]};不给则全测。须 admin(同 GET /config)。
        """
        _require_config_admin(caller, operation="config:test")
        from ducky.config_probe import probe_all
        secs = (body or {}).get("sections")
        return {"status": "ok", **probe_all(secs if isinstance(secs, list) else None)}

    @app.post("/config/_speed")
    def update_speed(body: dict, caller: str = "") -> dict:
        """在线微调 _speed 参数。body: {key, value} 或 {updates: {k:v,...}}。

        v22.0（雷霆审计 B7）：须 admin。
        """
        # f0.6 W-11/W-14：同 PUT /config，统一走 require_instance_admin。
        _require_config_admin(caller, operation="config:write")
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
    def change_password(body: dict, caller: str = "") -> dict:
        """修改 UI 登录口令。

        v19.4.1（P2-2 / P0-1）：
          · 哈希统一走 ducky.security.auth（PBKDF2-HMAC-SHA256 200k 轮），
            旧格式 `salt:sha256hex` 校验通过后自动升级，存量部署无感；
          · 改密成功后**撤销全部既有会话**，强制所有端重新登录 ——
            否则老会话仍能用旧凭据继续访问，改密等于没改。

        v22.0（雷霆审计 B7）：须 admin。判据与其它配置写面同一个 `_require_config_admin`
        （session 直通；Bearer 须绑定 + 名单）。此前这里单独用 `_is_admin_caller(caller)`：
        控制台改密不带 caller、默认部署也没有 admin 名单，于是主人登录后改密永远被拒。
        """
        _require_config_admin(caller, operation="config:write")
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
        resp = {
            "status": "ok",
            "detail": "密码已更新并即时生效，所有已登录会话已失效 / "
                      "Password updated; all existing sessions revoked",
            "sessions_revoked": revoked,
            "restart_required": False,
        }
        # f0.7.1：控制台改过的口令（source=user）会开启 API 门禁。没配 token 的回环部署，
        # 钩子 / MCP / cron 从此会 401 —— 明确告诉主人，而不是让接入方静默失联。
        if not os.environ.get("AIDUMEM_API_TOKEN", "").strip():
            resp["warning"] = ("API 门禁已随口令开启，但未设置 AIDUMEM_API_TOKEN：不带凭据的钩子、MCP、"
                               "定时任务将收到 401，请设置 token 并让接入方带上 / API gate is now on "
                               "but AIDUMEM_API_TOKEN is unset: credential-less hooks/MCP/cron will get 401")
            logger.warning("🔐 控制台改密开启了 API 门禁，但未设置 AIDUMEM_API_TOKEN —— 无凭据接入方将 401")
        return resp

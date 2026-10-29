"""ducky/config_probe.py — 设置页「测试连接」的真探针(f0.7 UI 体检)。

此前「测试连接」只打 /health,模型填错了也显示“连接正常”。这里对每个
已配置的模型段各发一次最小真实调用,回报 通/不通、耗时、出错原因:

- llm / vision:chat/completions,max_tokens=1;
- embedder:embeddings,回报向量维度;
- rerank:按配置的 provider 排两句话;
- decision:按配置的供应商做一次最小 memory_type 判断。

规矩:绝不回显密钥;尊重引擎挡位(本地档不外呼,如实回报);
每段独立超时、互不拖累;禁重定向。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict

_SECTIONS = ("llm", "vision", "embedder", "rerank", "decision")
_TIMEOUT = 15


def _raw_config() -> dict:
    from ducky.utils import mem0_config_path
    try:
        with open(mem0_config_path(), encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolved(raw: dict, section: str) -> dict:
    from ducky.mem0_runtime import _resolve_api_keys
    sec = raw.get(section) or {}
    try:
        res = _resolve_api_keys({section: {"provider": sec.get("provider"),
                                           "config": dict(sec.get("config") or {})}})
        return dict((res.get(section) or {}).get("config") or {})
    except (KeyError, TypeError, ValueError, AttributeError, OSError):
        return dict(sec.get("config") or {})


def _short(exc_or_text: Any) -> str:
    s = str(exc_or_text)
    for marker in ("Bearer ", "api_key", "Authorization"):
        if marker in s:
            s = s.split(marker)[0] + marker + "…"
    return s[:160]


def _http_error(r) -> str:
    try:
        body = r.json()
        msg = body.get("error") or body.get("detail") or body.get("message") or body
        if isinstance(msg, dict):
            msg = msg.get("message") or msg
    except (ValueError, AttributeError):
        msg = (r.text or "")[:120]
    return _short(f"HTTP {r.status_code}: {msg}")


def _friendly(exc: Exception) -> str:
    import requests
    from urllib.parse import urlsplit
    url = getattr(getattr(exc, "request", None), "url", "") or ""
    host = urlsplit(url).netloc or "该地址"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"超时:{host} {_TIMEOUT} 秒没回应"
    if isinstance(exc, requests.exceptions.SSLError):
        return f"证书不对:{host}"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return f"连不上 {host}(服务没开、地址或端口写错)"
    return _short(f"{type(exc).__name__}: {exc}")


def _models_hint(base: str, key: str) -> str:
    """模型名写错时,顺手列出对方实际挂着的模型(vLLM / OpenAI 兼容都有 /models)。"""
    import requests
    try:
        headers = {"Authorization": "Bearer " + key} if key else {}
        r = requests.get(base + "/models", headers=headers, timeout=5, allow_redirects=False)
        ids = [str(m.get("id")) for m in (r.json().get("data") or []) if isinstance(m, dict)][:6]
        return (";可用模型: " + ", ".join(ids)) if ids else ""
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return ""


def _post(url: str, key: str, payload: dict):
    import requests
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    return requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT,
                         allow_redirects=False)


def _probe_chat(cfg: dict) -> Dict[str, Any]:
    base = str(cfg.get("openai_base_url") or "").rstrip("/")
    if not base or not cfg.get("model"):
        return {"ok": False, "status": "not_configured", "detail": "未填模型或地址"}
    r = _post(base + "/chat/completions", str(cfg.get("api_key") or ""),
              {"model": cfg["model"], "max_tokens": 1,
               "messages": [{"role": "user", "content": "ping"}]})
    if r.status_code != 200:
        return {"ok": False, "status": "error",
                "detail": _http_error(r) + _models_hint(base, str(cfg.get("api_key") or ""))}
    data = r.json()
    if not data.get("choices"):
        return {"ok": False, "status": "error", "detail": "返回里没有 choices(不像 OpenAI 兼容接口)"}
    return {"ok": True, "status": "ok", "detail": "模型 " + str(data.get("model") or cfg["model"])}


def _probe_embed(cfg: dict) -> Dict[str, Any]:
    base = str(cfg.get("openai_base_url") or "").rstrip("/")
    if not base or not cfg.get("model"):
        return {"ok": False, "status": "not_configured", "detail": "未填模型或地址"}
    r = _post(base + "/embeddings", str(cfg.get("api_key") or ""),
              {"model": cfg["model"], "input": "ping"})
    if r.status_code != 200:
        return {"ok": False, "status": "error",
                "detail": _http_error(r) + _models_hint(base, str(cfg.get("api_key") or ""))}
    vec = ((r.json().get("data") or [{}])[0] or {}).get("embedding") or []
    if not vec:
        return {"ok": False, "status": "error", "detail": "返回里没有向量"}
    return {"ok": True, "status": "ok", "detail": f"向量 {len(vec)} 维"}


def _probe_rerank(raw: dict) -> Dict[str, Any]:
    from ducky.mem0_runtime import RERANK_PROVIDERS, _load_rerank_config
    cfg = _load_rerank_config()
    if cfg.get("config_error"):
        return {"ok": False, "status": "config_error", "detail": cfg["config_error"]}
    if not cfg.get("base_url") or not cfg.get("model"):
        return {"ok": False, "status": "not_configured", "detail": "未填模型或地址"}
    fn = RERANK_PROVIDERS.get(str(cfg.get("provider") or "").lower())
    if fn is None:
        return {"ok": False, "status": "config_error", "detail": f"不认识的 rerank 供应商: {cfg.get('provider')}"}
    out = fn(cfg, "小明喜欢什么", ["小明喜欢骑车", "今天下雨"], 2)
    if not out:
        return {"ok": False, "status": "error", "detail": "返回空结果"}
    note = "" if cfg.get("enabled") else "(已配置但未启用)"
    return {"ok": True, "status": "ok", "detail": f"{cfg.get('provider')} · 排序 {len(out)} 条{note}"}


def _probe_decision() -> Dict[str, Any]:
    from ducky import decision
    cfg = decision.settings()
    st = cfg.get("status")
    if st in ("not_configured", "config_error", "blocked_by_engine_mode"):
        return {"ok": False, "status": st, "detail": {"not_configured": "未配置",
                "config_error": "配置有误", "blocked_by_engine_mode": "本地档不外呼"}[st]}
    if not cfg.get("api_key"):
        return {"ok": False, "status": "not_configured", "detail": "缺 API Key"}
    probe_cfg = dict(cfg, enabled=True, status="ready", users=[],
                     tasks=dict(cfg.get("tasks") or {}, memory_type=True))
    from ducky.memory_types import TYPE_LABELS
    answers, info = decision.decide(
        "memory_type", {"text": "连接测试:小明喜欢骑车 " + str(time.time())},
        {"type": {"type": "choice", "instructions": "Classify the main meaning of this memory.",
                  "criteria": TYPE_LABELS}},
        "__config_probe__", "default", probe_cfg)
    status = (info or {}).get("status", "")
    if answers:
        note = "" if cfg.get("enabled") else "(已配置但未启用)"
        return {"ok": True, "status": "ok", "detail": f"{cfg.get('provider')} · {cfg.get('model')}{note}"}
    reasons = {"error_fallback": "调用失败:密钥、地址或模型不对,或供应商连不上",
               "circuit_open": "最近连续失败,熔断冷却中(稍后再测)",
               "busy_fallback": "决策通道忙,稍后再测",
               "invalid_response": "供应商回答格式不对"}
    return {"ok": False, "status": status or "error",
            "detail": reasons.get(status, _short(status or "供应商无有效回答"))}


def probe(section: str) -> Dict[str, Any]:
    """测一段。永不抛异常;返回 {section, ok, status, detail, ms}。"""
    import requests
    from ducky.engine_mode import cloud_egress_allowed
    t0 = time.monotonic()
    raw = _raw_config()
    try:
        if section != "decision" and not (raw.get(section) or {}).get("config"):
            out = {"ok": False, "status": "not_configured",
                   "detail": "未单独配置(与 LLM 共用)" if section == "vision" else "未配置"}
        elif not cloud_egress_allowed("config_probe"):
            out = {"ok": False, "status": "blocked_by_engine_mode", "detail": "本地档不外呼"}
        elif section in ("llm", "vision"):
            out = _probe_chat(_resolved(raw, section))
        elif section == "embedder":
            out = _probe_embed(_resolved(raw, section))
        elif section == "rerank":
            out = _probe_rerank(raw)
        elif section == "decision":
            out = _probe_decision()
        else:
            out = {"ok": False, "status": "unknown_section", "detail": section}
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError,
            RuntimeError, OSError) as exc:  # 连接拒绝 / 超时 / DNS / 回包不像样
        out = {"ok": False, "status": "error", "detail": _friendly(exc)}
    out.update(section=section, ms=int((time.monotonic() - t0) * 1000))
    return out


def probe_all(sections=None) -> Dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor
    secs = [s for s in (sections or _SECTIONS) if s in _SECTIONS]
    with ThreadPoolExecutor(max_workers=len(secs) or 1) as ex:
        results = list(ex.map(probe, secs))
    return {"results": results, "all_ok": all(r["ok"] or r["status"] == "not_configured" for r in results)}

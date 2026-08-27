"""aiduMEM speed · 会话合并队列（Mnemosyne 潮浪并忆）"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ducky.utils import DEFAULT_USER_ID
from ducky.speed.config import load_speed_cfg, messages_to_text
from ducky.speed.fastpath import try_fastpath_text
from ducky.speed.jobs import job_update
from ducky.speed.stats import (
    _record_wave_from_batch,
    coalesce_stats_snapshot,
    record_coalesce_enqueue,
)

logger = logging.getLogger("aiduMEM.speed")

# 设计：仅 async；键=user+session+profile；idle/window/满额冲刷；潮浪统计
_coalesce_lock = threading.Lock()
_coalesce_buf: dict[str, dict] = {}
_coalesce_worker_started = False
_coalesce_worker_lock = threading.Lock()
_coalesce_flush_cb: Optional[Callable] = None

def _coalesce_key(user_id: str, metadata: Optional[dict] = None, profile: str = "default",
                  bank_id: str = "default") -> str:
    """缓冲键 = user + **bank** + session + profile，避免亲密/技术短句混批。

    v20.2.4（外审 F-04）：此前键里**没有 bank** —— 同 user、同 session 的
    bank-a 与 bank-b 拿到同一个键 `u::s`，两个域的短句被合进同一批，
    第二个缓冲的 parts 里躺着另一个域的内容（外审实测）。
    """
    md = metadata or {}
    sid = (
        md.get("session_id")
        or md.get("session")
        or md.get("chat_id")
        or md.get("conversation_id")
        or ""
    )
    sid = str(sid).strip()
    prof = (profile or "default").strip() or "default"
    bank = (bank_id or "default").strip() or "default"
    base = f"{user_id}@{bank}::{sid}" if sid else f"{user_id or DEFAULT_USER_ID}@{bank}"
    if prof and prof != "default":
        return f"{base}::{prof}"
    return base


def resolve_coalesce_profile(metadata: Optional[dict] = None) -> str:
    """
    解析合并策略：
      1) metadata.coalesce_profile / coalesce_mode / profile 显式指定
      2) category 映射
      3) source / caller 映射
      4) 默认 default
    合法值：default | tech | intimate（未知名回落 default，但保留自定义 profiles 键）
    """
    md = dict(metadata or {})
    cfg = load_speed_cfg()
    profiles = cfg.get("coalesce_profiles") or {}
    default_name = str(cfg.get("coalesce_default_profile") or "default")

    explicit = (
        md.get("coalesce_profile")
        or md.get("coalesce_mode")
        or md.get("profile")
        or ""
    )
    explicit = str(explicit).strip().lower()
    # 别名
    aliases = {
        "int": "intimate",
        "love": "intimate",
        "diary": "intimate",
        "soft": "intimate",
        "fast": "tech",
        "code": "tech",
        "dev": "tech",
        "normal": "default",
        "chat": "default",
    }
    if explicit:
        name = aliases.get(explicit, explicit)
        if name in profiles or name in ("default", "tech", "intimate"):
            return name
        return default_name

    cat = str(md.get("category") or "").strip().lower()
    src = str(md.get("source") or md.get("caller") or "").strip().lower()
    by_cat = {str(k).lower(): str(v) for k, v in (cfg.get("coalesce_profile_by_category") or {}).items()}
    by_src = {str(k).lower(): str(v) for k, v in (cfg.get("coalesce_profile_by_source") or {}).items()}
    if cat and cat in by_cat:
        return by_cat[cat]
    if src and src in by_src:
        return by_src[src]
    return default_name


def _coalesce_cfg(metadata: Optional[dict] = None, profile: Optional[str] = None) -> dict:
    """按 profile 解析合并参数；metadata 可覆盖 profile。"""
    cfg = load_speed_cfg()
    prof = profile or resolve_coalesce_profile(metadata)
    profiles = cfg.get("coalesce_profiles") or {}
    # 兼容：无 profiles 时用顶层 coalesce_* 作为 default
    base = {
        "window_sec": float(cfg.get("coalesce_window_sec", 12)),
        "idle_sec": float(cfg.get("coalesce_idle_sec", 4)),
        "max_parts": int(cfg.get("coalesce_max_parts", 8)),
        "max_chars": int(cfg.get("coalesce_max_chars", 2000)),
        "max_single_chars": int(cfg.get("coalesce_max_single_chars", 500)),
    }
    overlay = {}
    if isinstance(profiles, dict):
        overlay = dict(profiles.get(prof) or profiles.get("default") or {})
    merged = {**base, **{k: overlay[k] for k in overlay if overlay[k] is not None}}

    return {
        "enabled": bool(cfg.get("coalesce_enabled", True)),
        "profile": prof,
        "window": float(merged.get("window_sec", base["window_sec"])),
        "idle": float(merged.get("idle_sec", base["idle_sec"])),
        "max_parts": int(merged.get("max_parts", base["max_parts"])),
        "max_chars": int(merged.get("max_chars", base["max_chars"])),
        "max_single": int(merged.get("max_single_chars", base["max_single_chars"])),
        "tick": float(cfg.get("coalesce_flush_tick_sec", 0.5)),
    }


def _buf_snapshot(key: str, buf: dict) -> dict:
    parts = list(buf.get("parts") or [])
    text = "\n".join(parts)
    return {
        "key": key,
        "user_id": buf.get("user_id"),
        # batch 自带的**不可变** scope —— 冲刷时只用这个，绝不回头读全局状态
        "bank_id": buf.get("bank_id") or "default",
        "profile": buf.get("profile") or "default",
        "messages": [{"role": "user", "content": text}],
        "metadata": dict(buf.get("metadata") or {}),
        "job_ids": list(buf.get("job_ids") or []),
        "parts": parts,
        "count": len(parts),
        "chars": len(text),
        "first_ts": buf.get("first_ts"),
        "last_ts": buf.get("last_ts"),
        "reason": buf.get("_flush_reason") or "manual",
        "idle_sec": buf.get("idle_sec"),
        "window_sec": buf.get("window_sec"),
    }


def _should_flush_locked(buf: dict, now: float, cfg: Optional[dict] = None) -> Optional[str]:
    """优先用缓冲自带的 idle/window（创建时锁定 profile 参数）。"""
    if not buf or not buf.get("parts"):
        return None
    cfg = cfg or {}
    idle_v = buf.get("idle_sec")
    if idle_v is None:
        idle_v = cfg.get("idle", 4)
    window_v = buf.get("window_sec")
    if window_v is None:
        window_v = cfg.get("window", 12)
    max_parts_v = buf.get("max_parts")
    if max_parts_v is None:
        max_parts_v = cfg.get("max_parts", 8)
    max_chars_v = buf.get("max_chars")
    if max_chars_v is None:
        max_chars_v = cfg.get("max_chars", 2000)
    idle = float(idle_v)
    window = float(window_v)
    max_parts = int(max_parts_v)
    max_chars = int(max_chars_v)
    if now - float(buf.get("last_ts") or now) >= idle:
        return "idle"
    if now - float(buf.get("first_ts") or now) >= window:
        return "window"
    if len(buf["parts"]) >= max_parts:
        return "max_parts"
    chars = sum(len(p) for p in buf["parts"])
    if chars >= max_chars:
        return "max_chars"
    return None


def coalesce_should_buffer(
    user_id: str,
    messages_json,
    metadata: Optional[dict] = None,
    *,
    async_mode: bool = False,
) -> tuple[bool, str]:
    """是否应进入合并队列。返回 (yes, reason)。"""
    md = dict(metadata or {})
    cfg = _coalesce_cfg(md)
    if not cfg["enabled"] or cfg["window"] <= 0:
        return False, "disabled"
    if not async_mode:
        return False, "sync_path"
    if md.get("no_coalesce") or md.get("force_sync") or md.get("coalesce") is False:
        return False, "opt_out"
    text = messages_to_text(messages_json)
    if not text:
        return False, "empty"
    if len(text) > cfg["max_single"]:
        return False, "too_long"
    # 明确结构化事实句可直接 fastpath，不必等合并
    # 若调用方显式 no_fastpath，则仍走合并（调试/强制 LLM 场景）
    if not md.get("no_fastpath") and try_fastpath_text(text):
        return False, "fastpath_candidate"
    return True, f"ok:{cfg['profile']}"


def coalesce_enqueue(
    user_id: str,
    messages_json,
    metadata: Optional[dict] = None,
    *,
    job_id: Optional[str] = None,
    # v20.2.4（外审 F-04）：scope 随 batch 走，**不靠回调闭包捕获**。
    # 全局 _coalesce_flush_cb 会被后一个请求覆盖，闭包里那个 bank_id 是
    # 「最后一次注册时的」，冲刷时可能把 A 域的内容写进 B 域。
    bank_id: str = "default",
) -> dict:
    """
    把一条短句放入合并缓冲。
    返回：
      buffered=True  → 已入队等待；可能顺带 ready 一条旧缓冲
      merged_ready   → 本次触发立即冲刷（条数/字数满），附 messages
    """
    md = dict(metadata or {})
    cfg = _coalesce_cfg(md)
    profile = cfg["profile"]
    text = messages_to_text(messages_json)
    # 写入 metadata 便于落库/调试
    md.setdefault("coalesce_profile", profile)
    key = _coalesce_key(user_id, md, profile, bank_id=bank_id)
    now = time.time()
    ready_batch = None

    with _coalesce_lock:
        buf = _coalesce_buf.get(key)

        # 若旧缓冲已到期，先摘出（用缓冲自身 profile 参数）
        if buf:
            reason = _should_flush_locked(buf, now, cfg)
            if reason in ("idle", "window"):
                buf["_flush_reason"] = reason
                ready_batch = _buf_snapshot(key, buf)
                del _coalesce_buf[key]
                buf = None

        if not buf:
            _coalesce_buf[key] = {
                "user_id": user_id,
                "bank_id": (bank_id or "default").strip() or "default",
                "profile": profile,
                "first_ts": now,
                "last_ts": now,
                "parts": [text],
                "metadata": md,
                "job_ids": [job_id] if job_id else [],
                "idle_sec": cfg["idle"],
                "window_sec": cfg["window"],
                "max_parts": cfg["max_parts"],
                "max_chars": cfg["max_chars"],
            }
            count = 1
            chars = len(text)
        else:
            # 同 key 已锁定 profile；后写 metadata 补充缺失字段，但不覆盖先写的值
            buf["parts"].append(text)
            buf["last_ts"] = now
            if md:
                # 只补充 buf["metadata"] 中还不存在的 key，不覆盖已有的
                for k, v in md.items():
                    if k not in buf["metadata"]:
                        buf["metadata"][k] = v
                buf["metadata"]["coalesce_profile"] = buf.get("profile") or profile
            if job_id:
                buf.setdefault("job_ids", []).append(job_id)
            count = len(buf["parts"])
            chars = sum(len(p) for p in buf["parts"])

        # 追加后立刻满额 → 本次直接 ready（含当前句）
        immediate = None
        cur = _coalesce_buf.get(key)
        if cur:
            max_parts = int(cur.get("max_parts") or cfg["max_parts"])
            max_chars = int(cur.get("max_chars") or cfg["max_chars"])
            if count >= max_parts:
                cur["_flush_reason"] = "max_parts"
                immediate = _buf_snapshot(key, cur)
                del _coalesce_buf[key]
            elif chars >= max_chars:
                cur["_flush_reason"] = "max_chars"
                immediate = _buf_snapshot(key, cur)
                del _coalesce_buf[key]

    out = {
        "buffered": immediate is None,
        "count": count if immediate is None else immediate["count"],
        "chars": chars if immediate is None else immediate["chars"],
        "key": key,
        "profile": profile,
        "merged_ready": bool(ready_batch or immediate),
        "idle_sec": cfg["idle"],
        "window_sec": cfg["window"],
    }
    # 入队命中：本次短句进入合并路径（无论是否立刻满额冲刷）
    try:
        record_coalesce_enqueue(profile)
    except Exception as e:
        logger.debug(f"record coalesce enqueue skip: {e}")

    # 优先返回“满额即时包”；旧到期包由 worker/调用方另处理
    batch = immediate or ready_batch
    if batch:
        bprof = batch.get("profile") or profile
        out["messages"] = batch["messages"]
        out["metadata"] = {
            **batch["metadata"],
            "coalesced": True,
            "coalesce_count": batch["count"],
            "coalesce_reason": batch["reason"],
            "coalesce_job_ids": batch["job_ids"],
            "coalesce_profile": bprof,
        }
        out["job_ids"] = batch["job_ids"]
        out["flush_reason"] = batch["reason"]
        out["user_id"] = batch["user_id"]
        out["profile"] = bprof
        # 即时/顺带冲刷 → 记潮浪
        try:
            _record_wave_from_batch({
                **batch,
                "key": key if batch is immediate else batch.get("key", key),
                "user_id": batch.get("user_id") or user_id,
                "profile": bprof,
                "reason": batch.get("reason") or out.get("flush_reason"),
                "count": batch.get("count"),
            })
        except Exception as e:
            logger.debug(f"record immediate wave skip: {e}")
        if ready_batch and immediate and ready_batch is not immediate:
            try:
                _record_wave_from_batch({
                    **ready_batch,
                    "key": ready_batch.get("key") or key,
                    "user_id": ready_batch.get("user_id") or user_id,
                    "profile": ready_batch.get("profile") or profile,
                    "reason": ready_batch.get("reason"),
                    "count": ready_batch.get("count"),
                })
            except Exception as e:
                logger.debug(f"record also_ready wave skip: {e}")
            out["also_ready"] = [{
                "user_id": ready_batch["user_id"],
                "messages": ready_batch["messages"],
                "metadata": {
                    **ready_batch["metadata"],
                    "coalesced": True,
                    "coalesce_count": ready_batch["count"],
                    "coalesce_reason": ready_batch["reason"],
                    "coalesce_job_ids": ready_batch["job_ids"],
                    "coalesce_profile": ready_batch.get("profile") or profile,
                },
                "job_ids": ready_batch["job_ids"],
            }]
    return out


def coalesce_flush_due(
    user_id: Optional[str] = None,
    *,
    force: bool = False,
    key: Optional[str] = None,
    bank_id: Optional[str] = None,
) -> list[dict]:
    """
    冲刷到期缓冲。
    返回 [{user_id, bank_id, messages, metadata, job_ids, count, reason, key, profile}, ...]

    v20.2.4（外审 F-04）：
    · 新增 bank_id 过滤 —— 手动冲刷不该把一个域的内容交给另一个域处理；
    · **键格式已变**（`{user}@{bank}[::sid][::prof]`），这里的前缀匹配同步改掉。
      改了键的构造却忘了改读取方，症状是「冲刷不到任何东西且不报错」。
    """
    now = time.time()
    out: list[dict] = []
    with _coalesce_lock:
        if key:
            keys = [key]
        elif user_id and bank_id:
            _pfx = f"{user_id}@{(bank_id or 'default').strip() or 'default'}"
            keys = [k for k in _coalesce_buf.keys() if k == _pfx or k.startswith(f"{_pfx}::")]
        elif user_id:
            _pfx = f"{user_id}@"
            keys = [k for k in _coalesce_buf.keys() if k.startswith(_pfx)]
        else:
            keys = list(_coalesce_buf.keys())
        for k in keys:
            buf = _coalesce_buf.get(k)
            if not buf:
                continue
            reason = "force" if force else _should_flush_locked(buf, now)
            if not reason:
                continue
            buf["_flush_reason"] = reason
            snap = _buf_snapshot(k, buf)
            bprof = snap.get("profile") or "default"
            out.append({
                "key": k,
                "user_id": snap["user_id"],
                # F-04：scope 随 batch 走 —— 这一处此前漏了，于是 _buf_snapshot
                # 带上了 bank 而 flush_due 又把它丢掉，冲刷方拿到 None。
                "bank_id": snap.get("bank_id") or "default",
                "profile": bprof,
                "messages": snap["messages"],
                "metadata": {
                    **snap["metadata"],
                    "coalesced": True,
                    "coalesce_count": snap["count"],
                    "coalesce_reason": reason,
                    "coalesce_job_ids": snap["job_ids"],
                    "coalesce_profile": bprof,
                },
                "job_ids": snap["job_ids"],
                "count": snap["count"],
                "chars": snap["chars"],
                "reason": reason,
            })
            del _coalesce_buf[k]
    # 锁外记潮浪（每次 flush 一批 = 一浪）
    for batch in out:
        _record_wave_from_batch(batch)
    return out


def coalesce_status(user_id: Optional[str] = None) -> dict:
    """运维/调试：当前缓冲水位 + 各 profile 参数。"""
    base = _coalesce_cfg()
    cfg_all = load_speed_cfg()
    profiles_out = {}
    for name in ("default", "tech", "intimate"):
        c = _coalesce_cfg(profile=name)
        profiles_out[name] = {
            "window_sec": c["window"],
            "idle_sec": c["idle"],
            "max_parts": c["max_parts"],
            "max_chars": c["max_chars"],
            "max_single_chars": c["max_single"],
        }
    # 自定义 profile 也列一下
    for name in (cfg_all.get("coalesce_profiles") or {}):
        if name not in profiles_out:
            c = _coalesce_cfg(profile=name)
            profiles_out[name] = {
                "window_sec": c["window"],
                "idle_sec": c["idle"],
                "max_parts": c["max_parts"],
                "max_chars": c["max_chars"],
                "max_single_chars": c["max_single"],
            }

    now = time.time()
    items = []
    with _coalesce_lock:
        for k, buf in _coalesce_buf.items():
            if user_id and not (k == user_id or k.startswith(f"{user_id}::")):
                continue
            parts = list(buf.get("parts") or [])
            items.append({
                "key": k,
                "user_id": buf.get("user_id"),
                "profile": buf.get("profile") or "default",
                "count": len(parts),
                "chars": sum(len(p) for p in parts),
                "age_sec": round(now - float(buf.get("first_ts") or now), 2),
                "idle_sec": round(now - float(buf.get("last_ts") or now), 2),
                "cfg_idle_sec": buf.get("idle_sec"),
                "cfg_window_sec": buf.get("window_sec"),
                "preview": (parts[-1] if parts else "")[:80],
                "job_ids": list(buf.get("job_ids") or []),
            })
    snap = coalesce_stats_snapshot()
    return {
        "enabled": base["enabled"],
        "default_profile": cfg_all.get("coalesce_default_profile", "default"),
        "window_sec": base["window"],
        "idle_sec": base["idle"],
        "max_parts": base["max_parts"],
        "max_chars": base["max_chars"],
        "profiles": profiles_out,
        "buffers": items,
        "buffer_count": len(items),
        "worker_started": _coalesce_worker_started,
        "hit_stats": snap.get("summary"),
        "last_waves": (snap.get("last_waves") or [])[-5:],
    }


def register_coalesce_flusher(cb: Callable) -> None:
    """注册冲刷回调：cb(user_id, messages, metadata, job_ids, *, bank_id)。

    v20.2.4（外审 F-04）：bank_id 是**参数**而不是闭包变量 —— 这个回调是
    进程级单例，后注册的会覆盖先注册的，闭包里捕获的 scope 属于「最后那次
    请求」，冲刷别人的 batch 时就写错域了。
    """
    global _coalesce_flush_cb
    _coalesce_flush_cb = cb


def _coalesce_worker_loop() -> None:
    while True:
        try:
            cfg = _coalesce_cfg()
            time.sleep(max(0.2, cfg["tick"]))
            if not cfg["enabled"]:
                continue
            due = coalesce_flush_due()
            if not due:
                continue
            cb = _coalesce_flush_cb
            for batch in due:
                jids = batch.get("job_ids") or []
                if cb:
                    try:
                        cb(
                            batch["user_id"],
                            batch["messages"],
                            batch["metadata"],
                            jids,
                            bank_id=batch.get("bank_id") or "default",
                        )
                    except Exception as e:
                        logger.error(f"coalesce flush cb failed: {e}")
                        for jid in jids:
                            job_update(jid, status="error", error=f"coalesce flush: {e}"[:300])
                else:
                    # 无回调时至少标记 job，避免永远 queued
                    for jid in jids:
                        job_update(
                            jid,
                            status="error",
                            error="coalesce flusher not registered",
                        )
        except Exception as e:
            logger.debug(f"coalesce worker tick skip: {e}")
            time.sleep(1.0)


def ensure_coalesce_worker() -> None:
    """启动后台 idle 冲刷线程（只一次）。"""
    global _coalesce_worker_started
    with _coalesce_worker_lock:
        if _coalesce_worker_started:
            return
        t = threading.Thread(
            target=_coalesce_worker_loop,
            name="aiduMEM-coalesce-flush",
            daemon=True,
        )
        t.start()
        _coalesce_worker_started = True
        logger.info("✅ coalesce worker started")


def coalesce_note(user_id: str, messages_json, metadata: dict) -> dict:
    """旧接口兼容 → coalesce_enqueue（不创建 job）。"""
    return coalesce_enqueue(user_id, messages_json, metadata or {})


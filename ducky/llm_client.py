"""
ducky.llm_client — 轻量共享 LLM 调用助手（v19.0）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reflect / 记忆去重自编辑 等认知层模块需要一个「不进 mem0 抽取管道」的
直接 LLM 调用通道：读 mem0_config_local.json 里的 llm 配置，复用
与 mem0_runtime 完全相同的密钥解析规则（__SF_KEY__ / __LLM_KEY__ /
key 文件回退），通过 requests 直发 OpenAI 兼容 chat/completions。

铁律：密钥永远从占位符文件解析，不在源码里硬编码。
失败一律返回 None，由调用方降级，不阻断主链路。

f0.3（A4）：**进程级 LLM 并发闸门**。生产上游（StepFun）限并发 5，一天回
「concurrency reached, current: 6, limit: 5」约 134 次 —— 服务里 call_llm 与
mem0 抽取两条通道各自想发就发，进程内没有任何总量约束。现在两条通道共用本模块
一个 BoundedSemaphore（AIDUMEI_LLM_MAX_CONCURRENCY，默认 4，给上游其他消费者
留 1 个余量），等位带超时（AIDUMEI_LLM_SLOT_TIMEOUT_SEC，默认 120s）：等不到就
按一次 LLM 失败处理（call_llm 回 None；mem0 通道抛 LLMConcurrencyTimeout，mem0
会把它包成 LLMError，走写入链路既有的「LLM 故障 → 确定性直写」降级），绝不死锁。

f0.3（A5）：两条通道每次调用的结局（ok / failed / gate_timeout）按小时记进
<DATA_DIR>/llm_outcomes.json（跨进程文件锁 + 原子替换，保留 8 天），
scripts/report.py 据此算「24h LLM 降级率」。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Optional

import requests

from ducky.env_config import float_env, int_env
from ducky.utils import BASE_DIR, mem0_config_path

try:  # 跨进程文件锁（POSIX）；Windows 上退化为进程内线程锁
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台
    fcntl = None

logger = logging.getLogger("aiduMEM.llm_client")

# ── f0.3（A4）：进程级并发闸门 ────────────────────────────────────────────
_MAX_CONCURRENCY_ENV = "AIDUMEI_LLM_MAX_CONCURRENCY"
_SLOT_TIMEOUT_ENV = "AIDUMEI_LLM_SLOT_TIMEOUT_SEC"
DEFAULT_LLM_MAX_CONCURRENCY = 4
DEFAULT_LLM_SLOT_TIMEOUT_SEC = 120.0


def llm_gate_limit_from_env() -> int:
    """并发上限：有限整数且 ≥1；NaN / 小数 / 乱码 / 0 一律回退默认并出声（env_config 纪律）。"""
    return int_env(_MAX_CONCURRENCY_ENV, DEFAULT_LLM_MAX_CONCURRENCY, minimum=1)


def llm_slot_timeout_from_env() -> float:
    return float_env(_SLOT_TIMEOUT_ENV, DEFAULT_LLM_SLOT_TIMEOUT_SEC, exclusive_minimum=0.0)


LLM_MAX_CONCURRENCY = llm_gate_limit_from_env()
LLM_SLOT_TIMEOUT_SEC = llm_slot_timeout_from_env()
_LLM_SLOTS = threading.BoundedSemaphore(LLM_MAX_CONCURRENCY)


class LLMConcurrencyTimeout(TimeoutError):
    """在超时时间内没等到进程级 LLM 空位。调用方按「一次 LLM 失败」处理。"""


@contextmanager
def llm_slot(origin: str = "call_llm"):
    """占一个进程级 LLM 空位；超时抛 LLMConcurrencyTimeout。

    闸门与超时都在调用时读模块全局（测试可替换）；释放的一定是本次占到的那一个。
    """
    slots, timeout = _LLM_SLOTS, LLM_SLOT_TIMEOUT_SEC
    if not slots.acquire(timeout=timeout):
        logger.warning("⏳ LLM 并发闸门：%.1fs 内没等到空位（上限 %d，调用方 %s）—— 按一次 LLM 失败处理",
                       timeout, LLM_MAX_CONCURRENCY, origin)
        raise LLMConcurrencyTimeout(
            f"no LLM slot within {timeout:.1f}s (limit {LLM_MAX_CONCURRENCY}, origin {origin})")
    try:
        yield
    finally:
        slots.release()


# ── f0.3（A5）：LLM 调用结局账本（按小时分桶，跨进程） ──────────────────────
LLM_OUTCOMES_FILE = "llm_outcomes.json"
OUTCOME_KINDS = ("ok", "failed", "gate_timeout")
_OUTCOME_KEEP_HOURS = 24 * 8
_outcome_thread_lock = threading.Lock()


def llm_outcomes_path() -> str:
    from ducky import utils as _utils
    return os.path.join(_utils.DATA_DIR, LLM_OUTCOMES_FILE)


def _hour_key(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H", time.gmtime(ts))


@contextmanager
def _outcome_file_lock(path: str):
    """进程内线程锁 + 跨进程 flock。锁文件以只读打开：flock 不需要写权限，
    另一身份（例如 root 的 cron）建出来的锁文件也照样锁得上。"""
    with _outcome_thread_lock:
        fd = None
        if fcntl is not None:
            try:
                fd = os.open(path + ".lock", os.O_RDONLY | os.O_CREAT, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                logger.debug("LLM 结局账本跨进程锁不可用，退化为进程内锁: %s", exc)
                if fd is not None:
                    os.close(fd)
                fd = None
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)  # 关闭即释放 flock


def _read_outcomes(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.debug("LLM 结局账本读不出（按空账处理）: %s", exc)
        return {}
    hours = data.get("hours") if isinstance(data, dict) else None
    return hours if isinstance(hours, dict) else {}


def _bump(hours: dict, key: str, origin: str, outcome: str) -> None:
    bucket = hours.get(key)
    if not isinstance(bucket, dict):
        bucket = hours[key] = {}
    counts = bucket.get(origin)
    if not isinstance(counts, dict):
        counts = bucket[origin] = {}
    counts[outcome] = int(counts.get(outcome) or 0) + 1


def record_llm_outcome(outcome: str, *, origin: str, now: Optional[float] = None) -> None:
    """记一次 LLM 调用结局。记账失败只降级为 debug，绝不影响调用本身。"""
    if outcome not in OUTCOME_KINDS:
        outcome = "failed"
    ts = time.time() if now is None else now
    path = llm_outcomes_path()
    try:
        with _outcome_file_lock(path):
            hours = _read_outcomes(path)
            _bump(hours, _hour_key(ts), str(origin or "unknown"), outcome)
            cutoff = _hour_key(ts - _OUTCOME_KEEP_HOURS * 3600)
            kept = {k: v for k, v in hours.items() if str(k) >= cutoff}
            tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"schema": 1, "hours": kept}, fh, sort_keys=True)
            os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as exc:
        logger.debug("LLM 结局记账跳过: %s", exc)


def llm_outcome_window(hours: int = 24, *, now: Optional[float] = None,
                       path: Optional[str] = None) -> dict:
    """最近 `hours` 个小时桶（含当前小时）的结局汇总，给 report.py 用。"""
    ts = time.time() if now is None else now
    target = path or llm_outcomes_path()
    buckets = _read_outcomes(target)
    totals = {k: 0 for k in OUTCOME_KINDS}
    by_origin: dict = {}
    for key in {_hour_key(ts - h * 3600) for h in range(max(int(hours), 1))}:
        bucket = buckets.get(key)
        if not isinstance(bucket, dict):
            continue
        for origin, counts in bucket.items():
            if not isinstance(counts, dict):
                continue
            mine = by_origin.setdefault(str(origin), {k: 0 for k in OUTCOME_KINDS})
            for kind in OUTCOME_KINDS:
                n = counts.get(kind)
                n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0
                mine[kind] += n
                totals[kind] += n
    return {"window_hours": int(hours), **totals, "total": sum(totals.values()),
            "by_origin": by_origin, "ledger_present": os.path.exists(target)}

MEM0_CONFIG = mem0_config_path()   # v20.2.4 F-22：支持 AIDUMEM_CONFIG_FILE

# 密钥占位符 → 对应 key 文件（顺序即回退顺序）
_KEY_FALLBACKS = {
    "llm": [os.path.join(BASE_DIR, ".llm_key"), os.path.join(BASE_DIR, ".sensenova_key")],
    "embedding": [os.path.join(BASE_DIR, ".sf_key")],
}

_config_cache: Optional[dict] = None
_config_lock = threading.Lock()

# ── 认知类调用的首试输出预算（v20 · P1-5 根因整改）────────────────────────────
# 推理模型的「思考」和「输出」共享同一个 max_tokens：预算给小了，思考先把它
# 吃光，content 回空 + finish_reason=length（v19.4.0 生产实测 🔴-B）。
#
# 为什么从 512 抬到 1024，不是拍的：`call_llm` 的截断重试按 ×4 放大且封顶 4096，
# 512 的重试上限只到 2048 —— 而实测过的空串悬崖就在 2000 附近，也就是说旧值连
# 「兜底那一次」都还踩在悬崖里侧。1024 让兜底一次直接顶到 4096，越过悬崖。
#
# 评估（governance）／自编辑（self_edit）／蒸馏（instinct_graduation）三处认知
# 调用全部指向这一个常量：预算是一个决定，不是三份抄写。
COGNITIVE_MAX_TOKENS = 1024


def get_llm_config() -> dict:
    """读取 mem0_config 的 llm 段，解析密钥占位符。结果缓存，进程内只读一次。

    失败不缓存：配置文件缺失/损坏时返回空配置，但不会把空配置写进缓存，
    下一次调用会重新尝试读取（配置修复后无需重启进程即可恢复）。
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    with _config_lock:
        # 双检：等锁期间可能已被并发调用方填充
        if _config_cache is not None:
            return _config_cache

        cfg: dict = {"model": "", "base_url": "", "api_key": ""}
        try:
            if not os.path.exists(MEM0_CONFIG):
                logger.warning("mem0_config_local.json 不存在，LLM 配置为空")
                return cfg

            with open(MEM0_CONFIG, encoding="utf-8") as f:
                raw = json.load(f)

            llm_cfg = raw.get("llm", {}).get("config", {}) or {}
            cfg["model"] = llm_cfg.get("model", "")
            cfg["base_url"] = llm_cfg.get("openai_base_url", "")

            api_key = llm_cfg.get("api_key", "")
            # v20.4.0-alpha（P1-7）：env 覆盖与 mem0_runtime 同权同优先级 ——
            # 两条读取链（SDK 初始化 / 直连通道）不许出现「一边 env 生效一边不生效」。
            env_key = os.environ.get("AIDUMEI_LLM_API_KEY", "").strip()
            cfg["api_key"] = env_key if env_key else _resolve_key(api_key, "llm")
            _config_cache = cfg
        except Exception as e:
            logger.warning(f"读取 LLM 配置失败（下次调用重试）: {e}")
        return cfg


def _resolve_key(api_key: str, purpose: str) -> str:
    """把占位符解析成真实密钥；已是真实 key 则原样返回。"""
    placeholders = {"__SF_KEY__", "__LLM_KEY__", "__EMBED_KEY__"}
    if api_key and api_key not in placeholders:
        return api_key

    for key_file in _KEY_FALLBACKS.get(purpose, []):
        if os.path.exists(key_file):
            try:
                with open(key_file, encoding="utf-8") as f:
                    resolved = f.read().strip()
                if resolved:
                    return resolved
            except OSError:
                continue
    return api_key if api_key not in placeholders else ""


def _extract_content(data: dict) -> Optional[str]:
    """从一个 chat/completions 响应对象里提取 assistant 文本。

    兼容非流式（choices[0].message.content）与流式块（choices[0].delta.content）。
    """
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    message = choice.get("message") or choice.get("delta") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if content:
        return str(content).strip()
    return None


def _parse_completion_body(text: str) -> Optional[str]:
    """解析 chat/completions 响应体（v19.4.0 · 生产审计 🔴-B）。

    上游网关实测会返回 Content-Type: text/event-stream，body 却是
    「完整 JSON + data: [DONE]」拼接体，r.json() 直接抛异常——
    v19.4.0 评估器因此永远记 evaluator_unavailable。兜底策略：

      1. 标准 JSON → 直接提取
      2. 拼接体 / 真 SSE 流 → 逐行剥 `data: ` 前缀、跳过 [DONE]：
         · 出现完整响应对象 → 取 message.content
         · 全是 delta 块    → 拼接 delta.content
    解析不出内容返回 None（调用方降级），绝不抛异常。
    """
    text = (text or "").strip()
    if not text:
        return None
    # 1) 标准 JSON 直通
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return _extract_content(data)
    except Exception:
        pass
    # 2) SSE / 拼接体逐行兜底
    full_parts: list[str] = []
    delta_chunks: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if not line or line == "[DONE]":
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        content = _extract_content(data)
        if content:
            choices = data.get("choices") or [{}]
            is_delta = isinstance(choices[0], dict) and "delta" in choices[0]
            (delta_chunks if is_delta else full_parts).append(content)
    if full_parts:
        return "\n".join(full_parts).strip() or None
    if delta_chunks:
        return "".join(delta_chunks).strip() or None
    return None


def _post_completion(endpoint: str, api_key: str, model: str, messages: list,
                     max_tokens: int, temperature: float, timeout: int) -> tuple[Optional[str], bool]:
    """发一次 chat/completions，返回 (content, 推理截断标志)。

    推理截断 = HTTP 200 但 content 为空、finish_reason=length 且响应带
    reasoning_content——推理模型把全部预算耗在思考上，没来得及输出正文。
    """
    r = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # 🔴-B：显式要求非流式。部分网关无视该字段仍回 SSE，
            # 响应体交给 _parse_completion_body 兜底解析。
            "stream": False,
        },
        timeout=timeout,
    )
    if r.status_code != 200:
        logger.warning(f"LLM 直接调用失败: HTTP {r.status_code} {r.text[:200]}")
        return None, False
    content = _parse_completion_body(r.text)
    if content:
        return content, False
    # 探测「推理截断」：content 空 + finish_reason=length + 有 reasoning_content
    try:
        data = json.loads(r.text.strip().splitlines()[0].removeprefix("data:").strip())
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        if choice.get("finish_reason") == "length" and msg.get("reasoning_content"):
            return None, True
    except Exception:
        pass
    logger.warning("LLM 直接调用: HTTP 200 但响应体解析不出内容: %s", r.text[:200])
    return None, False


def call_llm(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.3,
    timeout: int = 45,
) -> Optional[str]:
    """
    直接调用配置好的 LLM，返回 assistant 文本；失败返回 None（调用方降级）。

    Args:
        prompt: 用户消息内容
        system: 可选 system 提示
        max_tokens: 输出上限
        temperature: 采样温度（认知类任务用低温度求稳定）
        timeout: 请求超时秒数

    🔴-B 根治（v19.4.0 生产实测补强）：上游网关的推理模型，
    请求级 reasoning_effort/enable_thinking 均被网关无视；小预算下思考耗尽
    预算 → content 空 + finish_reason=length。检测到该形态自动放大预算重试一次。
    """
    # v20.2.4（外审 F-03）：**本地档在最底层硬阻断**，见
    # ducky/engine_mode.cloud_egress_allowed 的成因注释。返回 None 而不是抛异常
    # 是因为本函数的契约本来就是「失败返回 None，调用方降级」—— 于是阻断走的是
    # **既有的**降级路径，九个调用点一行都不用改。
    from ducky.engine_mode import cloud_egress_allowed
    if not cloud_egress_allowed("call_llm"):
        return None

    cfg = get_llm_config()
    if not cfg.get("api_key") or not cfg.get("model"):
        logger.debug("LLM 未配置，跳过直接调用")
        return None

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        return None

    # 兼容三种配置形态：纯域名、带 /v1 前缀、已指向 /chat/completions
    if base.endswith("/chat/completions"):
        endpoint = base
    else:
        endpoint = f"{base}/chat/completions"

    # f0.3：每一次真实外呼都先占进程级空位（与 mem0 抽取通道共用一个闸门），
    # 结局记进小时账本。上面几个早退（本地档阻断 / 未配置）是配置选择，不是降级，不记账。
    outcome = "failed"
    try:
        with llm_slot("call_llm"):
            content, reasoning_truncated = _post_completion(
                endpoint, cfg["api_key"], cfg["model"], messages,
                max_tokens, temperature, timeout)
        if content:
            outcome = "ok"
            return content
        if reasoning_truncated:
            retry_budget = min(max_tokens * 4, 4096)
            logger.info("LLM 推理截断（思考耗尽预算），放大预算重试: %d → %d",
                        max_tokens, retry_budget)
            with llm_slot("call_llm"):
                content, _ = _post_completion(
                    endpoint, cfg["api_key"], cfg["model"], messages,
                    retry_budget, temperature, timeout)
            if content:
                outcome = "ok"
                return content
        return None
    except LLMConcurrencyTimeout:
        outcome = "gate_timeout"   # 闸门已打过 warning；契约不变：失败回 None
        return None
    except Exception as e:
        logger.warning(f"LLM 直接调用异常: {e}")
        return None
    finally:
        record_llm_outcome(outcome, origin="call_llm")

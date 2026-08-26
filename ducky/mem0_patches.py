"""
mem0 运行时补丁层 —— v20.0pre

═══ 为什么存在 ═══
我们用 mem0ai 做记忆基座。基座上有三类缺陷要我们自己兜：

  A 类 · 上游至今未修：
    · Role Drop —— parse_messages() 只认 system/user/assistant，其余 role 的
      content 被静默丢弃，全程零告警。
    · 空抽取零日志 —— LLM 返回空串时 extracted_memories 直接置空，不打日志，
      记忆静静地没写进去，调用方拿到 200。
  B 类 · 上游已修、但仍需兼容旧版：
    · remove_code_blocks() 不吃 list 形态的 content（多模态消息格式），
      旧版直接 AttributeError: 'list' object has no attribute 'strip'。mem0ai
      2.0.19 已原生修复；本层仍负责旧版兜底与空抽取可观测性。
  C 类 · 我们自己写错的：
    · 用量追踪原本打在 Memory.client 上 —— 该属性不存在，补丁常年空转。

═══ 设计原则（对应 SOP 血泪铁律）═══
铁律 6 假绿灯
    每个补丁下手前先跑**运行时探针**，确认「这个缺陷此刻真的存在」。
    上游哪天修好了，探针会发现，补丁自动跳过并如实记账 —— 而不是盲目二次包装。
    副作用：本层对基座版本不敏感，看行为不看版本号。上游 main 的
    上游开发分支的元数据版本可能落后于实际源码，任何只看版本号的守卫都不可靠。
铁律 7 宣称即承诺
    不提供「已激活」这类无条件口号。只提供逐条状态，由 /health 对外暴露。
铁律 8 静默失败
    补丁失败、探针异常，全部进 patch_status()，一条都不吞。
铁律 13 配置写了≠配置生效
    `from mem0.memory.utils import parse_messages` 是**按名字绑定**：同一函数
    对象在 utils 和 main 两个命名空间各有独立引用。只替换 utils 那份，main
    里那份纹丝不动。已实测确认二者 `is` 同一对象，故必须逐命名空间替换。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
# §1  补丁台账（唯一真相源，/health 读它）
# ═══════════════════════════════════════════════
#   status 取值：
#     applied      补丁已生效
#     not_needed   探针证明缺陷不存在（上游已修）—— 正常，不是失败
#     drift        目标形状与预期不符，补丁未打 —— 需要人看
#     failed       打补丁过程本身抛异常 —— 需要人看
#     pending      还没跑到
_PATCH_LEDGER: dict[str, dict[str, Any]] = {}
_LEDGER_LOCK = threading.Lock()

# 运行时计数器：补丁「救回来了多少次」。0 也是信息 —— 说明这条路没走过。
_COUNTERS: dict[str, int] = {
    "role_drop_rescued": 0,      # 被救回的非标准 role 消息条数
    "empty_extraction": 0,       # LLM 抽取返回空的次数（原本静默）
    "list_content_normalized": 0,  # list 形态 content 被拍平的次数
    "llm_calls_tracked": 0,      # 用量追踪实际拦到的 LLM 调用数
    "embed_calls_tracked": 0,    # 用量追踪实际拦到的 embedding 调用数
}
_COUNTER_LOCK = threading.Lock()

# 已告警过的非标准 role，防日志洪水（每种 role 只 warning 一次）
_WARNED_ROLES: set[str] = set()

_PATCH_MARKER = "_aidumem_patched"


def _bump(key: str, n: int = 1) -> None:
    with _COUNTER_LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0) + n


def _record(pid: str, status: str, detail: str = "", **extra: Any) -> None:
    with _LEDGER_LOCK:
        _PATCH_LEDGER[pid] = {"status": status, "detail": detail, **extra}
    if status in ("drift", "failed"):
        logger.error("mem0 补丁 %s = %s：%s", pid, status, detail)
    elif status == "not_needed":
        logger.info("mem0 补丁 %s 无需应用：%s", pid, detail)
    else:
        logger.info("mem0 补丁 %s 已应用：%s", pid, detail)


def patch_status() -> dict[str, Any]:
    """给 /health 用。ok=False 表示有补丁 drift 或 failed，需要人介入。"""
    with _LEDGER_LOCK:
        ledger = {k: dict(v) for k, v in _PATCH_LEDGER.items()}
    with _COUNTER_LOCK:
        counters = dict(_COUNTERS)
    bad = [k for k, v in ledger.items() if v.get("status") in ("drift", "failed")]
    return {
        "ok": not bad and bool(ledger),
        "problems": bad,
        "patches": ledger,
        "counters": counters,
    }


def reset_for_test() -> None:
    """仅供测试：清空台账与计数，让同一进程内可以双向复现（铁律 14）。"""
    with _LEDGER_LOCK:
        _PATCH_LEDGER.clear()
    with _COUNTER_LOCK:
        for k in _COUNTERS:
            _COUNTERS[k] = 0
    _WARNED_ROLES.clear()


# ═══════════════════════════════════════════════
# §2  命名空间替换（铁律 13：按名字绑定要逐处替换）
# ═══════════════════════════════════════════════
_UTILS_CONSUMERS = ("mem0.memory.utils", "mem0.memory.main")


#: 打补丁前的原函数留底。**不是为了回滚** —— 是为了让「基线缺陷仍在吗」这类
#: 断言有一个不随执行顺序漂移的判据。
#:
#: v20 生产实机踩到的：三条 `test_baseline_*_defect_is_present_before_patching`
#: 读的是**当前绑定**的 `mem0.memory.utils.parse_messages`。补丁一旦在同进程里
#: 打过（顺序取决于哪个测试先 import 了运行时），基线用例看到的就是打过补丁的
#: 版本，于是报「基座已经修好了，补丁可以退役」—— 一条**说反了**的红灯。
#: 本地全绿、实机三条红，差别只是收集顺序。
_ORIGINALS: dict[str, Callable] = {}


def original(func_name: str) -> Callable | None:
    """取打补丁前的那一份；没打过补丁时返回 None（调用方自行取当前绑定）。"""
    return _ORIGINALS.get(func_name)


#: 补丁 id → 它接管的函数名。幂等分支要靠它现取覆盖状态。
_PID_FUNC = {"role_drop": "parse_messages", "code_block": "remove_code_blocks"}


def _bound_namespaces(func_name: str) -> list[str]:
    """这个名字现在绑着我们这一份的命名空间有哪些 —— 报**状态**，不报增量。

    v20 生产实机踩到：幂等分支原先只记一句「已在位」，`namespaces` 留空。
    于是同进程里第二次调用之后台账显示覆盖为 `[]`，而补丁完全生效 ——
    守卫读到「覆盖不全」，报的是一件根本没发生的事。
    """
    import sys
    out = []
    for m in _UTILS_CONSUMERS:
        f = getattr(sys.modules.get(m), func_name, None)
        if f is not None and getattr(f, _PATCH_MARKER, None):
            out.append(m)
    return out


def _rebind(func_name: str, new_func: Callable) -> list[str]:
    """把 new_func 绑到所有持有该名字的模块上，返回实际替换到的命名空间列表。"""
    import importlib
    import sys

    # 先留底（只留第一次的那一份 —— 重复打补丁不许把留底覆盖成已打补丁的版本）
    if func_name not in _ORIGINALS:
        try:
            mu = importlib.import_module("mem0.memory.utils")
            cur = getattr(mu, func_name, None)
            if cur is not None:
                _ORIGINALS[func_name] = cur
        except Exception:
            pass

    hit = []
    for modname in _UTILS_CONSUMERS:
        mod = sys.modules.get(modname)
        if mod is None:
            try:
                mod = importlib.import_module(modname)
            except Exception as e:  # 模块导不进来 = 基座结构变了，必须让人知道
                logger.error("补丁重绑失败，无法导入 %s：%s", modname, e)
                continue
        if hasattr(mod, func_name):
            setattr(mod, func_name, new_func)
        # ⚠️ 记「现在绑在哪」，不是「这次换了几处」。
        #
        # v20 生产实机踩到：同进程里补丁被打第二次时，第一次已经把两个命名空间
        # 都换好了，于是这一轮一处都没「换」，`namespaces` 报 `[]` —— 而补丁其实
        # 完全生效（同一轮里前两条断言都过了）。台账报的是**增量**，读者当它是
        # **状态**，于是「覆盖不全」这条红灯说的是一件根本没发生的事。
        # 判据改成现取现看：这个名字现在是不是绑着我们这一份。
        if getattr(mod, func_name, None) is new_func:
            hit.append(modname)
    return hit


# ═══════════════════════════════════════════════
# §3  补丁 role_drop —— 非标准 role 不再静默丢弃
# ═══════════════════════════════════════════════
_PROBE_ROLE = "aidumem_probe_role"
_PROBE_TEXT = "aidumem_probe_payload_zzz"


def _patch_role_drop() -> None:
    pid = "role_drop"
    try:
        from mem0.memory import utils as mu

        orig = getattr(mu, "parse_messages", None)
        if orig is None:
            _record(pid, "drift", "mem0.memory.utils.parse_messages 不存在")
            return
        if getattr(orig, _PATCH_MARKER, None) == pid:
            _record(pid, "applied", "已在位（幂等跳过）",
                    namespaces=_bound_namespaces(_PID_FUNC.get(pid, "")))
            return

        # 探针：喂一条非标准 role，看 content 会不会被丢
        probe = orig([{"role": _PROBE_ROLE, "content": _PROBE_TEXT}])
        if _PROBE_TEXT in probe:
            _record(pid, "not_needed", "上游已处理非标准 role，探针确认 content 未丢失")
            return

        def parse_messages(messages):  # noqa: ANN001, ANN201
            response = ""
            for msg in messages:
                role = msg.get("role")
                content = msg.get("content")
                # 上游语义保留：没有文本内容的消息（如只带 tool_calls 的
                # assistant 消息）本就该跳过，不算 Role Drop。
                if content is None:
                    continue
                if role == "system":
                    response += f"system: {content}\n"
                elif role == "user":
                    response += f"user: {content}\n"
                elif role == "assistant":
                    response += f"assistant: {content}\n"
                else:
                    # ← 上游缺的 else。不再静默丢弃，且首次遇到该 role 时告警。
                    label = role if role else "unknown"
                    if label not in _WARNED_ROLES:
                        _WARNED_ROLES.add(label)
                        logger.warning(
                            "mem0 抽取遇到非标准 role=%r，已纳入抽取（上游默认会静默丢弃）", label
                        )
                    _bump("role_drop_rescued")
                    response += f"{label}: {content}\n"
            return response

        setattr(parse_messages, _PATCH_MARKER, pid)
        hit = _rebind("parse_messages", parse_messages)
        if not hit:
            _record(pid, "drift", "没有任何命名空间持有 parse_messages")
            return
        _record(pid, "applied", f"非标准 role 纳入抽取 + 首次告警；命名空间={hit}",
                namespaces=hit)
    except Exception as e:
        _record(pid, "failed", f"{type(e).__name__}: {e}")


# ═══════════════════════════════════════════════
# §4  补丁 code_block_hardening
#     一处补两件事：
#       ① 兼容旧基座的 list-content 形状（2.0.19+ 直接委托上游）
#       ② 空返回时打日志 —— 把 A1「空抽取零日志」变成可见、可计数
# ═══════════════════════════════════════════════
def _patch_code_block_hardening() -> None:
    pid = "code_block_hardening"
    try:
        from mem0.memory import utils as mu

        orig = getattr(mu, "remove_code_blocks", None)
        if orig is None:
            _record(pid, "drift", "mem0.memory.utils.remove_code_blocks 不存在")
            return
        if getattr(orig, _PATCH_MARKER, None) == pid:
            upstream_bug = getattr(orig, "_aidumem_upstream_list_bug_present", None)
            _record(pid, "applied", "已在位（幂等跳过）",
                    namespaces=_bound_namespaces(_PID_FUNC.get(pid, "")),
                    upstream_list_bug_present=upstream_bug)
            return

        # 探针①：list 形态会不会炸
        list_broken = False
        try:
            orig([{"type": "text", "text": _PROBE_TEXT}])
        except Exception:
            list_broken = True

        # 探针②：确认基座对 str 的行为没变（负向对照，防止我们包错了对象）
        try:
            if orig(f"```json\n{_PROBE_TEXT}\n```") != _PROBE_TEXT:
                _record(pid, "drift", "基座 remove_code_blocks 对 str 的行为与预期不符，不敢包")
                return
        except Exception as e:
            _record(pid, "drift", f"基座 remove_code_blocks 连 str 都处理不了：{type(e).__name__}: {e}")
            return

        def _flatten_legacy(content):  # noqa: ANN001, ANN202
            """给仍会对 list 调用 ``.strip()`` 的旧基座提供兼容文本。"""
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        t = item.get("text") or item.get("content")
                        if isinstance(t, str):
                            parts.append(t)
                _bump("list_content_normalized")
                return "\n".join(parts)
            return content

        def _prepare_list(content):  # noqa: ANN001, ANN202
            """只在必要时适配 list，不能覆盖上游已定义的拼接语义。

            mem0ai 2.0.19 按 ``text`` 块无分隔符拼接。标准 list 直接交给它；
            旧版则先拍成字符串。对 aiduMEI 历史上额外接受的 ``content`` 键，
            只做形状转换，不自行插入换行。
            """
            if not isinstance(content, list):
                return content
            if list_broken:
                return _flatten_legacy(content)
            if not any(
                isinstance(item, dict)
                and "text" not in item
                and isinstance(item.get("content"), str)
                for item in content
            ):
                return content
            normalized = []
            for item in content:
                if (
                    isinstance(item, dict)
                    and "text" not in item
                    and isinstance(item.get("content"), str)
                ):
                    normalized.append({**item, "text": item["content"]})
                else:
                    normalized.append(item)
            _bump("list_content_normalized")
            return normalized

        def remove_code_blocks(content):  # noqa: ANN001, ANN201
            prepared = _prepare_list(content)
            out = orig(prepared)
            if not out or not out.strip():
                # ← A1 现场。原本这里一片寂静，调用方只看到「没抽出记忆」。
                _bump("empty_extraction")
                source_length = len(content) if isinstance(content, (str, list, tuple)) else 0
                logger.warning(
                    "mem0 抽取返回空内容（原文 %d 字符，去壳后为空）—— "
                    "本次不会写入任何记忆。常见成因：推理模型 max_tokens 不足、"
                    "内容被模型侧拦截、prompt 触发空回。",
                    source_length,
                )
            return out

        setattr(remove_code_blocks, _PATCH_MARKER, pid)
        setattr(remove_code_blocks, "_aidumem_upstream_list_bug_present", list_broken)
        hit = _rebind("remove_code_blocks", remove_code_blocks)
        if not hit:
            _record(pid, "drift", "没有任何命名空间持有 remove_code_blocks")
            return
        _record(
            pid, "applied",
            f"list-content {'兼容兜底' if list_broken else '使用基座原生处理'}"
            f" + 空抽取告警；命名空间={hit}",
            namespaces=hit, upstream_list_bug_present=list_broken,
        )
    except Exception as e:
        _record(pid, "failed", f"{type(e).__name__}: {e}")


# ═══════════════════════════════════════════════
# §5  补丁 usage_tracking —— 打在真实挂载点上，用正确的调用形状
#     旧写法两处都错：
#       ① getattr(mem, "client") —— Memory 没有这个属性，补丁常年空转
#       ② _orig_create(self, ...) —— 多传一个位置参数，
#          TypeError: create() takes 1 argument(s) but 2 were given，
#          根本到不了网络。因①死得早，②从未暴露。
# ═══════════════════════════════════════════════
def _patch_usage_tracking(mem_instance: Any) -> None:
    pid = "usage_tracking"
    try:
        from ducky import mem0_runtime as rt
    except Exception as e:
        _record(pid, "failed", f"无法导入用量记账函数：{type(e).__name__}: {e}")
        return

    targets = [
        ("llm", "chat.completions", "_track_llm_usage", "llm_calls_tracked"),
        ("embedding_model", "embeddings", "_track_embed_usage", "embed_calls_tracked"),
    ]
    done, missing = [], []

    for attr, path, tracker_name, counter in targets:
        try:
            holder = getattr(mem_instance, attr, None)
            if holder is None:
                missing.append(f"Memory.{attr} 不存在")
                continue
            client = getattr(holder, "client", None)
            if client is None:
                missing.append(f"Memory.{attr}.client 不存在")
                continue

            node = client
            for seg in path.split("."):
                node = getattr(node, seg, None)
                if node is None:
                    break
            if node is None or not callable(getattr(node, "create", None)):
                missing.append(f"Memory.{attr}.client.{path}.create 不可调用")
                continue

            orig_create = node.create
            if getattr(orig_create, _PATCH_MARKER, None) == pid:
                done.append(f"{attr}（幂等跳过）")
                continue
            tracker = getattr(rt, tracker_name)

            # 闭包写法：不走 __get__，不传 self —— 这是活体对照证明过的正确形状。
            def _make(orig, track, cnt, is_llm):  # noqa: ANN001, ANN202
                def _tracked(*args, **kwargs):
                    resp = orig(*args, **kwargs)
                    usage = getattr(resp, "usage", None)
                    if usage:
                        try:
                            if is_llm:
                                track(
                                    getattr(usage, "prompt_tokens", 0) or 0,
                                    getattr(usage, "completion_tokens", 0) or 0,
                                    getattr(usage, "total_tokens", 0) or 0,
                                )
                            else:
                                track(getattr(usage, "total_tokens", 0) or 0)
                            _bump(cnt)
                        except Exception as te:
                            # 记账失败不许连坐业务调用，但必须留痕（铁律 8）
                            logger.warning("用量记账失败（不影响本次调用）：%s", te)
                    return resp

                setattr(_tracked, _PATCH_MARKER, pid)
                return _tracked

            node.create = _make(orig_create, tracker, counter, attr == "llm")
            done.append(attr)
        except Exception as e:
            missing.append(f"{attr}: {type(e).__name__}: {e}")

    if done and not missing:
        _record(pid, "applied", f"挂载点={done}", mounted=done)
    elif done:
        _record(pid, "drift", f"部分挂上：成功={done}，失败={missing}",
                mounted=done, missing=missing)
    else:
        _record(pid, "drift", f"一个都没挂上：{missing}", missing=missing)


# ═══════════════════════════════════════════════
# §6  统一入口
# ═══════════════════════════════════════════════


# ═══════════════════════════════════════════════
# §6  补丁 llm_transport_policy（v20.2.2 · LLM 腿挡位化 WP-I）
#     实弹取证（2026-08-26 网关 521 瞬态）：openai SDK 默认 max_retries=2
#     且尊重响应头 Retry-After: 120 —— mem0 内部 LLM 抽取把单次 /add
#     同步挂了 4.5 分钟。重试的职责上移给挡位（ducky.gear LLM 腿）与
#     降级链：传输层只许失败一次、限时返回。
#     45s 对齐 ducky/llm_client.py 已运行多版的验证值（推理模型蒸馏
#     上界），connect 10s —— 不是拍脑袋，是对齐自家栈的既验证参数。
#     ⚠️ 顺序契约：本补丁必须在 usage_tracking **之前**跑 ——
#     with_options 返回新客户端实例，若先 wrap 旧实例的 create 再换
#     客户端，用量追踪就静默空转（本文件 §5 头注记载的同款死法）。
# ═══════════════════════════════════════════════
def _patch_llm_transport_policy(mem_instance: Any) -> None:
    pid = "llm_transport_policy"
    try:
        llm = getattr(mem_instance, "llm", None)
        client = getattr(llm, "client", None)
        if client is None or not hasattr(client, "with_options"):
            _record(pid, "drift",
                    "mem0 LLM 客户端形态变化：Memory.llm.client 缺失或无 "
                    "with_options —— 补丁失去着力点，盲重试仍在")
            return
        import httpx
        llm.client = client.with_options(
            max_retries=0, timeout=httpx.Timeout(45.0, connect=10.0))
        _record(pid, "applied",
                "mem0 内部 LLM 客户端 max_retries=0 + timeout 45s/connect 10s"
                "（重试职责上移给挡位与降级链）")
    except Exception as e:
        _record(pid, "failed", f"{type(e).__name__}: {e}")


def apply_all(mem_instance: Any) -> dict[str, Any]:
    """在 Memory 实例造好之后调用一次。幂等。返回 patch_status()。"""
    _patch_role_drop()
    _patch_code_block_hardening()
    # 顺序契约：transport_policy 先换客户端实例，usage_tracking 再 wrap
    # 新实例的 create —— 反过来用量追踪会静默空转（§6 头注）。
    _patch_llm_transport_policy(mem_instance)
    _patch_usage_tracking(mem_instance)
    st = patch_status()
    if st["ok"]:
        logger.info("✅ mem0 补丁层就位：%s",
                    {k: v["status"] for k, v in st["patches"].items()})
    else:
        logger.error("⚠️ mem0 补丁层有问题项 %s：%s",
                     st["problems"], {k: v["status"] for k, v in st["patches"].items()})
    return st

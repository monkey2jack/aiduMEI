"""benchmarks.adapter — 评测适配器：对 aiduMEI 服务的真实 HTTP 契约。

设计约束（v20.0 权威方案 §4.3，逐条对应）：

1. 每次 HTTP 调用都有超时、状态码检查、响应 schema 检查、有限重试和
   request ID；401/403、5xx、超时、组件失败、空结果**分别计数**，
   绝不把任何一种失败变成「成功但零条」。
2. ``add_turn`` 走实际 ``/add`` 契约：``messages`` + ``user_id`` +
   ``metadata.recorded_at`` + 显式 ``force_sync=True``（force_sync 是
   **每次请求的 metadata 键**，不是服务端常驻配置）。若服务仍返回
   ``accepted``（异步回执），必须轮询 ``/add/job/{job_id}`` 直到 done，
   而不是看见 2xx 就当写成功。
3. ``search`` 走真实 ``/search``（显式搜索，不经过 ``/gate``），同时传
   ``limit`` 与 ``top_k``，解析 ``results`` 并原样保留 ``_recall_path``、
   memory id、时间与 provenance。**``/search`` 组件故障时返回的是
   HTTP 200 + body ``status:"error"``**——这正是「搜挂了」和「没搜到」
   的分界线，body 状态必须检查，故障必须抛错，不许静默当空结果。
4. 不实现、不伪造任何 ``benchmark_mode``：适配器不得暗中改变生产门控。
   v20 补注：``add_turn(infer=False)`` 不违反这一条 —— ``infer`` 是
   ``/add`` 的**公开契约字段**（``ducky/api_models.py``），由本适配器
   显式传入、由服务端回显确认，且只用于 G3b 的复现性自检；正式跑分
   （``--formal``）一律 ``infer=True``，与生产完全同路。区别在于：
   隐藏模式是「适配器偷偷换了被测系统的行为」，公开参数是「调用方
   要求了什么，回执里写着什么」。
5. case 隔离用哈希后的稳定命名空间：``user_id = bench-<dataset>-<h>``、
   ``bank_id = bench-<h>``（h = sha256(case_id) 前 16 位十六进制），
   原始题目内容不进日志、不进标识符。
6. 只有检索返回的证据才可交给答案模型（由 run.py 负责）；适配器保证
   证据链字段原样透传。

HTTP 只用标准库（urllib），评测工具不得给运行时引入未声明的**第三方**依赖。

唯一的仓内依赖是 ``ducky.utils.api_auth_headers``：鉴权头必须走产品自己的
**单一真相源**，适配器不许另抄一份读 token 的逻辑 —— 抄一份就是把同一条契约
写成两半，改一半忘一半（v20.0 甲3 的病根）。依赖方向是「工具 → 产品」，
不是反向，不给产品增加任何依赖；未配置 token 时它返回空 dict，
本机零配置的行为与门禁未启用时完全一致。
"""
from __future__ import annotations

import hashlib
import json
import logging
import socket
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

# 唯一的仓内依赖：鉴权头的单一真相源。见模块 docstring 的依赖说明。
from ducky.utils import api_auth_headers

logger = logging.getLogger("aiduMEM.benchmarks.adapter")

# 失败分类：统计口径与异常 kind 一一对应
KIND_AUTH = "auth"                 # 401/403
KIND_CLIENT = "client"             # 其他 4xx（如注入拦截 400）
KIND_SERVER = "server"             # 5xx（有限重试后仍失败）
KIND_TIMEOUT = "timeout"           # 连接/读取超时
KIND_PROTOCOL = "protocol"         # 非 JSON / 响应形状不符

# ── 说话人名不是 role ──────────────────────────────────────────────
# OpenAI 的 role 只有 system/user/assistant 三个合法值。LoCoMo 把说话人名
# （Melanie/Caroline）放在 speaker 字段，若直接当 role 传下去，服务端的
# mem0 抽取层 parse_messages() 只认那三个分支、**没有 else 也没有告警**，
# 整条消息静默落空 → 抽取提示词是空串 → 零事实入库，而 /add 照回 ok。
# 实测（2026-08-23，四组对照见 FINDING_role_drop.md）：
#   role=user 原句 Δ=+1 ／ role=Caroline Δ=0 ／ 同内容第一人称 Δ=0
#   ／ role=assistant Δ=0
# 即**只有 user 能产出语义记忆**。故此处把说话人名归一为 user，并把名字
# 前缀进正文以保住「谁说的」这一归属（实测抽出的事实确实保留了人名）。
# 已是合法 role 的原样透传 —— 不动确定性写入（G3b）的字节形态。
_WIRE_ROLES = frozenset({"system", "user", "assistant"})


def _normalize_turn(role: str, content: str) -> tuple[str, str, str]:
    """把 (role, content) 归一成可上线的 (wire_role, wire_content, speaker)。

    speaker 为空表示 role 本就合法、未做改写。
    """
    r = str(role).strip()
    c = str(content)
    if r.lower() in _WIRE_ROLES:
        return r.lower(), c, ""
    return "user", f"{r}: {c}", r


KIND_COMPONENT = "component_failure"  # HTTP 200 但 body status=error
KIND_JOB = "job_failed"            # 异步 job 以 error 收场或超时未完成
KIND_USAGE = "usage"               # 调用方违反适配器契约（如未 reset_case）


class AdapterError(RuntimeError):
    """带分类的适配器异常——每一类失败都能在统计里对上号。"""

    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 request_id: str = "", detail: Any = None) -> None:
        super().__init__(f"[{kind}] {message} (request_id={request_id or '-'})")
        self.kind = kind
        self.status = status
        self.request_id = request_id
        self.detail = detail


def case_namespace(dataset: str, case_id: str) -> tuple[str, str]:
    """哈希后的稳定命名空间：不把原始题目/个人内容放进标识符或日志。"""
    h = hashlib.sha256(str(case_id).encode("utf-8")).hexdigest()[:16]
    safe_ds = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(dataset))
    return f"bench-{safe_ds}-{h}", f"bench-{h}"


class AiduMEIBenchmarkAdapter:
    """对一个**正在运行的** aiduMEI HTTP 服务的评测适配器。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        job_poll_interval: float = 0.2,
        job_deadline: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.retry_backoff = float(retry_backoff)
        self.job_poll_interval = float(job_poll_interval)
        self.job_deadline = float(job_deadline)
        # case_id -> (user_id, bank_id)；强制先 reset_case 再写查
        self._cases: dict[str, tuple[str, str]] = {}
        self.stats: dict[str, int] = {
            "requests": 0,
            "retries": 0,
            "auth_errors": 0,
            "client_errors": 0,
            "server_errors": 0,
            "timeouts": 0,
            "protocol_errors": 0,
            "component_failures": 0,
            "job_failures": 0,
            "empty_results": 0,
        }

    # ── 传输层 ────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> tuple[dict, str]:
        """一次带重试的 HTTP 调用。返回 (解析后的 JSON, request_id)。

        重试只针对 5xx 与超时（幂等性由调用方保证：/search、/health、
        job 轮询天然幂等；/add 的重试语义见 add_turn 注释）。
        4xx 一律不重试——重复一个被拒绝的请求不会让它被接受。
        """
        request_id = uuid.uuid4().hex
        url = f"{self.base_url}{path}"
        data = None
        headers = {"X-Request-ID": request_id, "Accept": "application/json"}
        # 鉴权头取自产品的单一真相源，不在这里自带一份读 token 的实现（v20.0 甲3）。
        # 改造前这里一个 Authorization 都不发，而生产机门禁是开着的 ——
        # 拿它去打生产，第一个请求就 401：跑分不是分低，是压根跑不起来。
        # 每次请求都重新取，是为了让「运行中改配置」和测试里的 monkeypatch 都生效。
        headers.update(api_auth_headers())
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.stats["retries"] += 1
                time.sleep(self.retry_backoff * attempt)
            self.stats["requests"] += 1
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    status = resp.status
            except urllib.error.HTTPError as e:
                status = e.code
                detail = e.read().decode("utf-8", "replace")[:500]
                if status in (401, 403):
                    self.stats["auth_errors"] += 1
                    raise AdapterError(KIND_AUTH, f"HTTP {status} on {path}",
                                       status=status, request_id=request_id,
                                       detail=detail) from e
                if 400 <= status < 500:
                    self.stats["client_errors"] += 1
                    raise AdapterError(KIND_CLIENT, f"HTTP {status} on {path}",
                                       status=status, request_id=request_id,
                                       detail=detail) from e
                # 5xx：有限重试
                self.stats["server_errors"] += 1
                last_exc = AdapterError(KIND_SERVER, f"HTTP {status} on {path}",
                                        status=status, request_id=request_id,
                                        detail=detail)
                continue
            except (TimeoutError, socket.timeout) as e:
                self.stats["timeouts"] += 1
                last_exc = AdapterError(KIND_TIMEOUT, f"timeout on {path}",
                                        request_id=request_id)
                last_exc.__cause__ = e
                continue
            except urllib.error.URLError as e:
                # 连接失败与底层超时都从这里出来；reason 为 timeout 时归超时
                if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
                    self.stats["timeouts"] += 1
                    kind = KIND_TIMEOUT
                else:
                    self.stats["server_errors"] += 1
                    kind = KIND_SERVER
                last_exc = AdapterError(kind, f"{e.reason} on {path}",
                                        request_id=request_id)
                last_exc.__cause__ = e
                continue

            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception as e:
                self.stats["protocol_errors"] += 1
                raise AdapterError(KIND_PROTOCOL, f"non-JSON response on {path}",
                                   status=status, request_id=request_id,
                                   detail=body[:200]) from e
            if not isinstance(parsed, dict):
                self.stats["protocol_errors"] += 1
                raise AdapterError(KIND_PROTOCOL, f"non-object response on {path}",
                                   status=status, request_id=request_id,
                                   detail=str(parsed)[:200])
            return parsed, request_id

        assert last_exc is not None
        raise last_exc

    # ── 契约方法（§4.3 建议接口） ─────────────────────────────────

    def reset_case(self, dataset: str, case_id: str) -> None:
        """登记 case 命名空间并清空其作用域（确保干净起点）。"""
        scope = case_namespace(dataset, case_id)
        user_id, bank_id = scope
        resp, rid = self._request("POST", "/delete_all", {
            "user_id": user_id,
            "bank_id": bank_id,
            "confirm": True,
        })
        if resp.get("status") not in ("ok", "success"):
            raise AdapterError(KIND_PROTOCOL, "delete_all 未确认成功",
                               request_id=rid, detail=resp)
        self._cases[case_id] = scope

    def _scope(self, case_id: str) -> tuple[str, str]:
        scope = self._cases.get(case_id)
        if scope is None:
            raise AdapterError(
                KIND_USAGE,
                f"case 未初始化，必须先 reset_case（case_id hash="
                f"{hashlib.sha256(str(case_id).encode()).hexdigest()[:8]}）",
            )
        return scope

    def add_turn(
        self,
        case_id: str,
        session_id: str,
        turn_index: int,
        role: str,
        content: str,
        timestamp: str,
        dia_id: str = "",
        infer: bool = True,
    ) -> dict:
        """写入一轮对话。同步优先（force_sync），异步回执必须等 job 落定。

        注意重试语义：/add 非幂等，_request 的 5xx/超时重试可能造成
        重复写入；评测语境下「重复的记忆」只会让检索更难而非更容易，
        不会虚增成绩，故接受这一偏保守的取舍并留在协议里说明。

        ``dia_id``（v20 修）：LoCoMo 的证据标识（形如 ``D1:1``）。此前
        **从未灌进元数据**，而 run.py 的证据匹配器正是拿它去召回结果里
        找 —— 于是 LoCoMo 的 ``evidence_hits`` 结构性恒为空、召回诊断
        恒为 0.0。不是能力问题，是管线没接上。

        ``infer``（v20 新增）：False 请求服务端走免抽取确定性写入
        （PROTOCOL.md G3b）。**显式参数，不是隐藏模式**；且服务端必须
        回显 ``infer:false``，否则本方法抛协议错 —— 一个被静默忽略的
        确定性开关比没有开关更危险。
        """
        user_id, bank_id = self._scope(case_id)
        metadata = {
            "recorded_at": str(timestamp),
            "force_sync": True,
            "source": "benchmark",
            "bench_session_id": str(session_id),
            "bench_turn_index": int(turn_index),
        }
        if dia_id:
            metadata["bench_dia_id"] = str(dia_id)
        wire_role, wire_content, speaker = _normalize_turn(role, content)
        msg = {"role": wire_role, "content": wire_content}
        if speaker:
            # OpenAI 规范里说话人身份的正确位置；抽取层不读它，但留着让
            # 线上形态与已验证的探针逐字一致，也便于服务端日后利用。
            msg["name"] = speaker
            # 原始说话人名进元数据：逐字库与归属审计都靠它，不靠 role。
            metadata["bench_speaker"] = speaker
        payload = {
            "messages": [msg],
            "user_id": user_id,
            "bank_id": bank_id,
            # 显式传，不靠服务端默认值：与 force_sync 同一个理由
            "infer": bool(infer),
            "metadata": metadata,
        }
        resp, rid = self._request("POST", "/add", payload)

        if not infer and resp.get("infer") is not False:
            # 服务端没回显 infer=false ⇒ 无法证明它真的跳过了 LLM 抽取。
            # 此时若继续跑，G3b 的「bit 复现」断言就变成一句空话。
            self.stats["protocol_errors"] += 1
            raise AdapterError(
                KIND_PROTOCOL,
                "请求了 infer=false 但服务端未回显确认——不接受未经证实的确定性",
                request_id=rid, detail=resp,
            )

        if resp.get("status") == "accepted":
            # 服务端仍决定异步：轮询 job 直到 done/error（HTTP 202 不算完成）
            job_id = resp.get("job_id")
            if not job_id:
                self.stats["protocol_errors"] += 1
                raise AdapterError(KIND_PROTOCOL, "accepted 回执缺 job_id",
                                   request_id=rid, detail=resp)
            resp = self._wait_job(str(job_id), request_id=rid)
        elif resp.get("status") == "error":
            self.stats["component_failures"] += 1
            raise AdapterError(KIND_COMPONENT, "/add 返回 status=error",
                               request_id=rid, detail=resp)
        return {"response": resp, "request_id": rid,
                "user_id": user_id, "bank_id": bank_id}

    def _wait_job(self, job_id: str, *, request_id: str) -> dict:
        deadline = time.monotonic() + self.job_deadline
        while time.monotonic() < deadline:
            resp, rid = self._request("GET", f"/add/job/{job_id}")
            job = resp.get("job") or {}
            status = str(job.get("status") or "")
            if status == "done":
                return job.get("result") if isinstance(job.get("result"), dict) else job
            if status == "error":
                self.stats["job_failures"] += 1
                raise AdapterError(KIND_JOB, f"job {job_id} 失败",
                                   request_id=rid, detail=job.get("error"))
            time.sleep(self.job_poll_interval)
        self.stats["job_failures"] += 1
        raise AdapterError(KIND_JOB, f"job {job_id} 超时未完成",
                           request_id=request_id)

    def search(self, case_id: str, query: str, top_k: int = 5) -> dict:
        """真实 /search：body status=error 即组件故障，必须抛错。"""
        user_id, bank_id = self._scope(case_id)
        payload = {
            "query": str(query),
            "user_id": user_id,
            "bank_id": bank_id,
            # /search 同时接受两个字段；都传，杜绝「哪个生效」的歧义
            "limit": int(top_k),
            "top_k": int(top_k),
        }
        resp, rid = self._request("POST", "/search", payload)

        status = resp.get("status")
        if status == "error":
            self.stats["component_failures"] += 1
            raise AdapterError(
                KIND_COMPONENT,
                "/search 组件故障（status=error）——「搜挂了」不是「没搜到」",
                request_id=rid, detail=resp.get("detail"),
            )
        if status != "ok" or not isinstance(resp.get("results"), list):
            self.stats["protocol_errors"] += 1
            raise AdapterError(KIND_PROTOCOL, "/search 响应形状不符",
                               request_id=rid, detail=resp)

        results = resp["results"]
        if not results:
            # 诚实计数：空结果是合法结果，但必须可观测
            self.stats["empty_results"] += 1
        return {"results": results, "raw": resp, "request_id": rid,
                "user_id": user_id, "bank_id": bank_id}

    def health(self) -> dict:
        resp, rid = self._request("GET", "/health")
        return {"response": resp, "request_id": rid}

    def close_case(self, case_id: str) -> None:
        """清理 case 作用域并注销。清理失败必须暴露，不许静默。"""
        user_id, bank_id = self._scope(case_id)
        resp, rid = self._request("POST", "/delete_all", {
            "user_id": user_id,
            "bank_id": bank_id,
            "confirm": True,
        })
        if resp.get("status") not in ("ok", "success"):
            raise AdapterError(KIND_PROTOCOL, "close_case 清理未确认成功",
                               request_id=rid, detail=resp)
        self._cases.pop(case_id, None)

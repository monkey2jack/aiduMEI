"""v20 评测管线（benchmarks/）契约测试。

三层钉死：

1. schema 校验器——合法 fixture 通过；重复 question_id / 未知题型 /
   证据越界 / dia_id 重复必须拒绝；LoCoMo 上游已知标注问题进 anomalies
   而不是被静默修掉。
2. 适配器——对一个**线程内真实 HTTP 服务**（http.server）跑完整 urllib
   栈：同步写、异步回执必须等 job、/search 的 HTTP 200 + body error 必须
   抛错（组件故障 ≠ 空结果，这是负向对照）、5xx 有限重试后失败且计数、
   空结果诚实计数、X-Request-ID 随请求出门、delete_all 带 confirm；
   v20.0 甲3 新增：门禁开着时必须打得通**且服务端确实收到 Bearer**，
   抽走凭据必须响亮红在 auth 上（一正一反两条，互为对照）。
3. runner——digest 剥离簿记字段但不丢语义字段（含负向对照）；
   `--deterministic` 让每一条写入都走 infer=false 且闸门标识进 config
   （G3a/G3b 不可相撞），默认模式一条都不许偷偷免抽取；LongMemEval 注入
   强制排除 question_date 之后的会话（防泄漏）；未检索到证据时
   would_answer=False（未检索不作答）；LoCoMo 的 dia_id 必须真的进元数据
   并让证据召回命中（v20 修的回归钉）；无证据题记 N/A 而不是 0 分。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from benchmarks.adapter import (
    KIND_AUTH,
    AdapterError,
    AiduMEIBenchmarkAdapter,
    case_namespace,
)
from benchmarks.schemas import SchemaError, validate_locomo, validate_longmemeval


# ── 共用的合法样本构造 ──────────────────────────────────────────────

def _lme_instance(**over):
    inst = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "测试问题？",
        "answer": "测试答案",
        "question_date": "2023/05/20 (Sat) 10:00",
        "haystack_dates": ["2023/05/01 (Mon) 09:00"],
        "haystack_session_ids": ["s1"],
        "haystack_sessions": [[{"role": "user", "content": "内容"}]],
        "answer_session_ids": ["s1"],
    }
    inst.update(over)
    return inst


def _locomo_sample(**over):
    sample = {
        "sample_id": "sample1",
        "conversation": {
            "speaker_a": "A",
            "speaker_b": "B",
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "A", "dia_id": "D1:1", "text": "你好"},
                {"speaker": "B", "dia_id": "D1:2", "text": "你好呀"},
            ],
        },
        "qa": [
            {"question": "谁先打招呼？", "answer": "A", "category": 4,
             "evidence": ["D1:1"]},
        ],
    }
    sample.update(over)
    return sample


# ── 1. schema 校验器 ────────────────────────────────────────────────

def test_longmemeval_valid_instances_pass_with_live_counts():
    report = validate_longmemeval([
        _lme_instance(),
        _lme_instance(question_id="q2_abs", question_type="knowledge-update"),
    ])
    assert report["total"] == 2
    assert report["type_counts"] == {
        "single-session-user": 1, "knowledge-update": 1,
    }
    assert report["abstention"] == 1


def test_longmemeval_rejects_duplicate_question_id():
    with pytest.raises(SchemaError, match="question_id 重复"):
        validate_longmemeval([_lme_instance(), _lme_instance()])


def test_longmemeval_rejects_unknown_question_type():
    with pytest.raises(SchemaError, match="未知题型"):
        validate_longmemeval([_lme_instance(question_type="made-up-type")])


def test_longmemeval_rejects_evidence_outside_haystack():
    with pytest.raises(SchemaError, match="证据会话不在 haystack 内"):
        validate_longmemeval([_lme_instance(answer_session_ids=["ghost"])])


def test_longmemeval_rejects_misaligned_haystack_columns():
    with pytest.raises(SchemaError, match="haystack 长度不齐"):
        validate_longmemeval([_lme_instance(haystack_dates=[
            "2023/05/01 (Mon) 09:00", "2023/05/02 (Tue) 09:00",
        ])])


def test_longmemeval_strict_total_for_formal_files():
    with pytest.raises(SchemaError, match="官方每文件 500"):
        validate_longmemeval([_lme_instance()], expect_total=500)


def test_longmemeval_counts_sessions_after_question_date():
    inst = _lme_instance(
        haystack_dates=["2023/05/01 (Mon) 09:00", "2023/05/25 (Thu) 09:00"],
        haystack_session_ids=["s1", "s2"],
        haystack_sessions=[[{"role": "user", "content": "早"}],
                           [{"role": "user", "content": "晚——在提问之后"}]],
    )
    report = validate_longmemeval([inst])
    assert report["sessions_after_question"] == 1


def test_locomo_valid_sample_passes_with_live_category_counts():
    report = validate_locomo([_locomo_sample()])
    assert report["samples"] == 1
    assert report["qa_total"] == 1
    assert report["category_counts"] == {4: 1}
    assert report["anomalies"] == []


def test_locomo_rejects_duplicate_dia_id():
    sample = _locomo_sample()
    sample["conversation"]["session_1"][1]["dia_id"] = "D1:1"
    with pytest.raises(SchemaError, match="dia_id 重复"):
        validate_locomo([sample])


def test_locomo_rejects_unknown_category():
    sample = _locomo_sample()
    sample["qa"][0]["category"] = 9
    with pytest.raises(SchemaError, match="未知类别"):
        validate_locomo([sample])


def test_locomo_rejects_session_without_timestamp():
    sample = _locomo_sample()
    del sample["conversation"]["session_1_date_time"]
    with pytest.raises(SchemaError, match="缺 session_1_date_time"):
        validate_locomo([sample])


def test_locomo_cat5_adversarial_only_is_legal_but_missing_both_is_anomaly():
    ok = _locomo_sample()
    ok["qa"] = [{"question": "对抗题？", "adversarial_answer": "没提过",
                 "category": 5, "evidence": []}]
    assert validate_locomo([ok])["anomalies"] == []

    bad = _locomo_sample()
    bad["qa"] = [{"question": "对抗题？", "category": 5, "evidence": []}]
    report = validate_locomo([bad])
    # 上游数据不改：进 anomalies 如实上报，而不是 SchemaError 或静默修掉
    assert any("既无 answer 也无 adversarial_answer" in a
               for a in report["anomalies"])


def test_locomo_dangling_evidence_reported_not_silently_fixed():
    sample = _locomo_sample()
    sample["qa"][0]["evidence"] = ["D9:99"]
    report = validate_locomo([sample])
    assert any("D9:99" in a for a in report["anomalies"])


def test_committed_smoke_fixtures_are_schema_valid():
    """仓库里带的 fixture 必须永远过自己的校验器——宣称即承诺。"""
    import benchmarks.run as brun

    with open(f"{brun.FIXTURES_DIR}/smoke_longmemeval.json", encoding="utf-8") as f:
        lme_report = validate_longmemeval(json.load(f))
    assert lme_report["total"] == 2 and lme_report["abstention"] == 1
    assert lme_report["sessions_after_question"] == 1  # 防泄漏排除的靶子

    with open(f"{brun.FIXTURES_DIR}/smoke_locomo.json", encoding="utf-8") as f:
        loco_report = validate_locomo(json.load(f))
    assert loco_report["qa_total"] == 3
    assert loco_report["category_counts"] == {2: 1, 4: 1, 5: 1}
    assert loco_report["anomalies"] == []


def test_case_namespace_is_hashed_and_passes_bank_contract():
    """命名空间不泄漏题目原文，且必须能过 make_scope 的作用域校验。"""
    from ducky.bank_contract import make_scope

    user_id, bank_id = case_namespace("longmemeval", "q42_abs")
    assert "q42" not in user_id and "q42" not in bank_id
    scope = make_scope(user_id=user_id, bank_id=bank_id)
    assert scope.user_id == user_id and scope.bank_id == bank_id
    # 稳定性：同一 case 两次求值必须一致
    assert case_namespace("longmemeval", "q42_abs") == (user_id, bank_id)


# ── 2. 适配器 × 线程内真实 HTTP 服务 ────────────────────────────────

class _StubState:
    """可编程的服务替身状态：记录请求，按剧本回话。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.search_response = {"status": "ok", "results": []}
        self.add_mode = "sync"          # sync | accepted | error
        self.job_polls_until_done = 2   # 前 N-1 次 running，第 N 次 done
        self.fail_5xx_times = 0         # 前 N 次请求回 500
        # v20：真实服务端会回显 /add 的 infer。置 False 模拟「服务端把
        # 确定性开关静默吞了」——负向对照用。
        self.echo_infer = True
        # v20.0 甲3：门禁替身。置 True 后，任何不带
        # `Authorization: Bearer <expected_token>` 的请求一律 401 ——
        # 生产机上门禁就是开着的，而改造前适配器一个凭据都不发。
        # 这个开关让「跑分打不通生产」在测试里可复现，而不是只靠
        # grep 源码里出现过 api_auth_headers 这个名字就算交差。
        self.require_auth = False
        self.expected_token = ""
        self._job_seen: dict[str, int] = {}
        self._5xx_left = 0


class _StubHandler(BaseHTTPRequestHandler):
    state: _StubState  # 由夹具注入

    def log_message(self, *a):  # 静音
        pass

    def _reply(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, payload=None):
        self.state.requests.append({
            "method": self.command,
            "path": self.path,
            "request_id": self.headers.get("X-Request-ID", ""),
            # v20.0 甲3：把凭据头也记下来。断言必须能证明「服务端**收到**了
            # Bearer」，而不是只证明「客户端源码里出现过 api_auth_headers」。
            "authorization": self.headers.get("Authorization", ""),
            "payload": payload,
        })

    def _auth_ok(self) -> bool:
        """门禁替身的判据：与 api_server.py 的 http 中间件同形。

        require_auth 关着时永远放行 —— 其余用例（重试、job 轮询、组件故障）
        与鉴权无关，不该被这条改动牵连。
        """
        st = self.state
        if not st.require_auth:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {st.expected_token}"

    def do_GET(self):
        st = self.state
        # 门禁在路由之前（与 api_server.py 的 @app.middleware("http") 同序）
        if not self._auth_ok():
            self._record()
            return self._reply(401, {"error": "unauthorized"})
        if st._5xx_left > 0:
            st._5xx_left -= 1
            self._record()
            return self._reply(500, {"detail": "boom"})
        self._record()
        if self.path == "/health":
            return self._reply(200, {"status": "healthy", "module_ok": True})
        if self.path.startswith("/add/job/"):
            job_id = self.path.rsplit("/", 1)[-1]
            seen = st._job_seen.get(job_id, 0) + 1
            st._job_seen[job_id] = seen
            if seen >= st.job_polls_until_done:
                return self._reply(200, {"status": "ok", "job": {
                    "status": "done", "result": {"status": "ok"}}})
            return self._reply(200, {"status": "ok", "job": {"status": "running"}})
        return self._reply(404, {"detail": "not found"})

    def do_POST(self):
        st = self.state
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        # 门禁同样在路由之前；但 body 必须**先读干净**，否则客户端拿到的是
        # 连接重置而不是 401 —— 那测出来的就不是门禁，是一个假的传输错误。
        if not self._auth_ok():
            self._record(payload)
            return self._reply(401, {"error": "unauthorized"})
        if st._5xx_left > 0:
            st._5xx_left -= 1
            self._record(payload)
            return self._reply(500, {"detail": "boom"})
        self._record(payload)
        if self.path == "/delete_all":
            return self._reply(200, {"status": "ok", "details": {}})
        if self.path == "/add":
            if st.add_mode == "accepted":
                return self._reply(202, {"status": "accepted", "job_id": "job-1"})
            if st.add_mode == "error":
                return self._reply(200, {"status": "error", "detail": "pipeline down"})
            body = {"status": "ok", "action": "direct"}
            if st.echo_infer:
                body["infer"] = bool(payload.get("infer", True))
            return self._reply(200, body)
        if self.path == "/search":
            return self._reply(200, st.search_response)
        return self._reply(404, {"detail": "not found"})


@pytest.fixture()
def stub_server():
    state = _StubState()
    handler = type("Handler", (_StubHandler,), {"state": state})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state, base_url
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _adapter(base_url: str) -> AiduMEIBenchmarkAdapter:
    return AiduMEIBenchmarkAdapter(
        base_url, timeout=5.0, retry_backoff=0.01, job_poll_interval=0.01,
        job_deadline=5.0,
    )


def test_adapter_authenticates_against_a_gated_service(stub_server, monkeypatch):
    """★ v20.0 甲3 的**运行时证明**：门禁开着，适配器必须打得通。

    这条用例不看源码、不 grep 函数名 —— 它起一个**真的会 401** 的服务，
    让适配器把四条真实契约路径各打一次，然后断言两件事：

      · 客户端没吃到任何鉴权失败（`auth_errors == 0`，拿到了正常回执）；
      · **服务端确实收到了** `Authorization: Bearer <token>`。

    第二条才是关键。「源码里出现过 api_auth_headers」只是静态证据，
    而甲3 的缺陷形态恰恰是「静态看不出来」：适配器的地址是构造参数，
    门禁的存在也不在它的源码里。改造前跑这条会红在第一个 /delete_all 上 ——
    拿它去打生产，第一个请求就 401，那不是「跑分分低」，是压根跑不起来。

    token 用纯 ASCII：HTTP 头按 latin-1 编码，非 ASCII 的 token 会在
    urllib 里炸成 UnicodeEncodeError，那样红的是编码不是门禁。
    """
    state, base_url = stub_server
    state.require_auth = True
    state.expected_token = "bench-token-jia3"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "bench-token-jia3")

    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-auth")
    ad.add_turn("case-auth", "s1", 0, "user", "内容", "2023/05/01 09:00")
    state.search_response = {"status": "ok", "results": [{"memory": "x"}]}
    ad.search("case-auth", "查询")
    ad.health()

    assert ad.stats["auth_errors"] == 0, "门禁开着却算出鉴权失败 ⇒ 凭据没送到"
    assert state.requests, "服务端一个请求都没收到，这条用例什么也没证明"
    # 逐条点名核对，不用 any()：任何一条漏带凭据都是一次生产事故
    for r in state.requests:
        assert r["authorization"] == "Bearer bench-token-jia3", (
            f"{r['method']} {r['path']} 到达服务端时不带凭据"
        )
    # 四条路径都真的跑过 —— 否则上面的循环可能只覆盖了 /delete_all，
    # 「全都带凭据」就成了一句只在一个请求上成立的空话。
    assert {"/delete_all", "/add", "/search", "/health"} <= {
        r["path"] for r in state.requests
    }


def test_adapter_without_credentials_fails_loudly_on_auth(stub_server, monkeypatch, tmp_path):
    """★ 负向对照：把凭据抽走，必须**响亮地**红在 auth 上。

    这是上一条的对照组。没有它，上一条无法排除「服务端其实根本没在验」
    这个假绿：两条用例一正一反打同一个开关，才能证明验的是凭据本身。

    并且 401 必须落进 `auth_errors` 这个桶 —— 混进 client_errors 或被
    当成空结果，症状就变成「跑分分低」，而真相是「一条数据都没写进去」。

    AIDUMEM_ENV_FILE 指向一个不存在的路径：本机仓根没有 .env，但**生产沙箱
    里有**。不钉这一下，这条对照会在生产机上假绿（token 从 .env 兜底进来了）。
    """
    state, base_url = stub_server
    state.require_auth = True
    state.expected_token = "bench-token-jia3"
    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(tmp_path / "nonexistent.env"))

    ad = _adapter(base_url)
    with pytest.raises(AdapterError) as exc:
        ad.reset_case("longmemeval", "case-noauth")

    assert exc.value.kind == KIND_AUTH, "401 必须分类成 auth，不许混进别的桶"
    assert exc.value.status == 401
    assert ad.stats["auth_errors"] == 1
    # 4xx 一律不重试：重复一个被拒绝的请求不会让它被接受
    assert ad.stats["requests"] == 1 and ad.stats["retries"] == 0
    assert state.requests and state.requests[0]["authorization"] == "", (
        "对照组竟然带上了凭据 —— 那它对照的就不是「无凭据」这件事"
    )


def test_adapter_sync_add_flow_over_real_http(stub_server):
    state, base_url = stub_server
    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-1")
    out = ad.add_turn("case-1", "s1", 0, "user", "内容", "2023/05/01 09:00")
    assert out["response"]["status"] == "ok"

    reset_req = next(r for r in state.requests if r["path"] == "/delete_all")
    assert reset_req["payload"]["confirm"] is True, "delete_all 必须显式过闸"
    add_req = next(r for r in state.requests if r["path"] == "/add")
    md = add_req["payload"]["metadata"]
    assert md["force_sync"] is True and md["recorded_at"] == "2023/05/01 09:00"
    assert add_req["payload"]["user_id"].startswith("bench-longmemeval-")
    assert all(r["request_id"] for r in state.requests), "每个请求必须带 X-Request-ID"


def test_adapter_accepted_response_polls_job_until_done(stub_server):
    state, base_url = stub_server
    state.add_mode = "accepted"
    state.job_polls_until_done = 3
    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-2")
    out = ad.add_turn("case-2", "s1", 0, "user", "内容", "2023/05/01 09:00")
    assert out["response"]["status"] == "ok"
    polls = [r for r in state.requests if r["path"].startswith("/add/job/")]
    assert len(polls) == 3, "accepted 回执必须轮询到 done，2xx 不算写成功"


def test_adapter_add_component_error_raises_and_counts(stub_server):
    state, base_url = stub_server
    state.add_mode = "error"
    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-3")
    with pytest.raises(AdapterError) as exc:
        ad.add_turn("case-3", "s1", 0, "user", "内容", "2023/05/01 09:00")
    assert exc.value.kind == "component_failure"
    assert ad.stats["component_failures"] == 1


def test_adapter_search_body_error_is_failure_not_empty_results(stub_server):
    """负向对照：HTTP 200 + status=error 是「搜挂了」，不是「没搜到」。"""
    state, base_url = stub_server
    state.search_response = {"status": "error", "results": [], "detail": "组件炸了"}
    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-4")
    with pytest.raises(AdapterError) as exc:
        ad.search("case-4", "查询")
    assert exc.value.kind == "component_failure"
    assert ad.stats["component_failures"] == 1
    assert ad.stats["empty_results"] == 0, "组件故障绝不许折叠成空结果"


def test_adapter_empty_results_returned_honestly_and_counted(stub_server):
    state, base_url = stub_server
    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-5")
    out = ad.search("case-5", "查询")
    assert out["results"] == []
    assert ad.stats["empty_results"] == 1
    assert ad.stats["component_failures"] == 0


def test_adapter_5xx_retries_then_raises_with_counters(stub_server):
    state, base_url = stub_server
    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-6")
    state._5xx_left = 99  # 永远 5xx：重试耗尽后必须失败
    with pytest.raises(AdapterError) as exc:
        ad.search("case-6", "查询")
    assert exc.value.kind == "server"
    assert ad.stats["retries"] == ad.max_retries
    assert ad.stats["server_errors"] == ad.max_retries + 1


def test_adapter_5xx_recovers_within_retry_budget(stub_server):
    state, base_url = stub_server
    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-7")
    state._5xx_left = 1  # 第一次 500，第二次成功
    out = ad.search("case-7", "查询")
    assert out["results"] == []
    assert ad.stats["retries"] == 1 and ad.stats["server_errors"] == 1


def test_adapter_add_defaults_to_production_infer_true(stub_server):
    """默认必须是生产语义：显式传 infer=true，不靠服务端默认值。"""
    state, base_url = stub_server
    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-infer-default")
    ad.add_turn("case-infer-default", "s1", 0, "user", "内容", "2023/05/01 09:00")
    add_req = next(r for r in state.requests if r["path"] == "/add")
    assert add_req["payload"]["infer"] is True


def test_adapter_deterministic_add_requests_infer_false(stub_server):
    """G3b 通路：infer=false 必须作为**公开顶层字段**发出，并被回执确认。"""
    state, base_url = stub_server
    ad = _adapter(base_url)
    ad.reset_case("locomo", "case-det")
    out = ad.add_turn("case-det", "s1", 0, "A", "内容", "1:56 pm on 8 May, 2023",
                      dia_id="D1:1", infer=False)
    add_req = next(r for r in state.requests if r["path"] == "/add")
    assert add_req["payload"]["infer"] is False
    # dia_id 必须真的进元数据——这正是此前漏掉、导致召回恒 0 的那一环
    assert add_req["payload"]["metadata"]["bench_dia_id"] == "D1:1"
    assert out["response"]["infer"] is False
    assert ad.stats["protocol_errors"] == 0


def test_adapter_rejects_unconfirmed_determinism(stub_server):
    """负向对照：请求了 infer=false 而服务端不回显 ⇒ 抛 protocol 错。

    一个被静默忽略的确定性开关比没有开关更危险：它会让 G3b 的
    「bit 复现」断言在实际走着 LLM 抽取的通路上凭空「通过」。
    """
    state, base_url = stub_server
    state.echo_infer = False          # 服务端吞掉开关
    ad = _adapter(base_url)
    ad.reset_case("locomo", "case-det-bad")
    with pytest.raises(AdapterError) as exc:
        ad.add_turn("case-det-bad", "s1", 0, "A", "内容", "stamp", infer=False)
    assert exc.value.kind == "protocol"
    assert ad.stats["protocol_errors"] == 1

    # 同一个吞开关的服务端，默认（infer=true）通路不受影响：
    # 不要求回显 true，避免给旧调用方凭空加一条破坏性契约
    out = ad.add_turn("case-det-bad", "s1", 1, "A", "内容", "stamp")
    assert out["response"]["status"] == "ok"
    assert ad.stats["protocol_errors"] == 1


def test_adapter_omits_dia_id_metadata_when_absent(stub_server):
    """longmemeval 没有 dia_id：不许往元数据里塞空键污染 payload。"""
    state, base_url = stub_server
    ad = _adapter(base_url)
    ad.reset_case("longmemeval", "case-nodia")
    ad.add_turn("case-nodia", "s1", 0, "user", "内容", "2023/05/01 09:00")
    add_req = next(r for r in state.requests if r["path"] == "/add")
    assert "bench_dia_id" not in add_req["payload"]["metadata"]


def test_adapter_refuses_operations_without_reset_case(stub_server):
    _, base_url = stub_server
    ad = _adapter(base_url)
    with pytest.raises(AdapterError) as exc:
        ad.search("never-reset", "查询")
    assert exc.value.kind == "usage"


# ── 3. runner：防泄漏、未检索不作答、digest 复现 ────────────────────

def test_runner_excludes_sessions_after_question_date(stub_server):
    state, base_url = stub_server
    import benchmarks.run as brun

    with open(f"{brun.FIXTURES_DIR}/smoke_longmemeval.json", encoding="utf-8") as f:
        inst = json.load(f)[0]  # smoke_lme_001 带一个提问日期之后的会话
    ad = _adapter(base_url)
    record = brun.run_longmemeval_instance(ad, inst, top_k=5)
    assert record["excluded_sessions_after_question_date"] == ["smoke_s3_future"]
    assert "smoke_s3_future" not in record["injected_sessions"]
    added = [r["payload"]["metadata"]["bench_session_id"]
             for r in state.requests if r["path"] == "/add"]
    assert "smoke_s3_future" not in added, "防泄漏：未来会话一条都不许写入"


def test_runner_abstains_when_nothing_retrieved(stub_server):
    """未检索时不得回答：空结果 ⇒ would_answer=False。"""
    state, base_url = stub_server
    import benchmarks.run as brun

    with open(f"{brun.FIXTURES_DIR}/smoke_longmemeval.json", encoding="utf-8") as f:
        inst = json.load(f)[0]
    ad = _adapter(base_url)  # stub 默认返回空 results
    record = brun.run_longmemeval_instance(ad, inst, top_k=5)
    assert record["would_answer"] is False
    assert record["retrieved_evidence_only"] == []
    assert record["evidence_recall_diagnostic"] == 0.0


def test_runner_locomo_evidence_recall_uses_dia_id_metadata(stub_server, tmp_path):
    """v20 修的回归钉：dia_id 灌进元数据后，证据召回必须真的能命中。

    此前 ``add_turn`` 从不传 dia_id，匹配器却拿它去找 ——
    ``evidence_hits`` 结构性恒空、召回诊断恒 0.0。这是管线没接上，
    不是检索能力差；本测试让那种状态无法再次悄悄通过。
    """
    state, base_url = stub_server
    state.search_response = {"status": "ok", "results": [
        {"id": "m1", "memory": "学吉他",
         "metadata": {"bench_dia_id": "D1:1", "bench_session_id": "session_1"}},
    ]}
    import benchmarks.run as brun

    summary = brun.run_smoke(base_url, "locomo", top_k=5, out_dir=str(tmp_path))
    with open(summary["jsonl"], encoding="utf-8") as f:
        records = [json.loads(line) for line in f]

    by_qa = {r["qa_index"]: r for r in records}
    # QA0 证据 ["D1:1"] → 全中
    assert by_qa[0]["evidence_hits"] == ["D1:1"]
    assert by_qa[0]["evidence_recall_diagnostic"] == 1.0
    # QA1 证据 ["D1:1","D2:1"] → 中一半，必须是 0.5 而不是 0 或 1
    assert by_qa[1]["evidence_hits"] == ["D1:1"]
    assert by_qa[1]["evidence_recall_diagnostic"] == 0.5


def test_runner_no_evidence_case_is_na_not_zero(stub_server, tmp_path):
    """诚实性钉：无证据的对抗/弃答题记 N/A，且不被平均成 0 分。"""
    state, base_url = stub_server
    state.search_response = {"status": "ok", "results": [
        {"id": "m1", "memory": "学吉他", "metadata": {"bench_dia_id": "D1:1"}},
    ]}
    import benchmarks.run as brun

    summary = brun.run_smoke(base_url, "locomo", top_k=5, out_dir=str(tmp_path))
    with open(summary["jsonl"], encoding="utf-8") as f:
        records = [json.loads(line) for line in f]

    cat5 = next(r for r in records if r["category"] == 5)
    assert cat5["evidence_dia_ids"] == []
    assert cat5["evidence_recall_applicable"] is False
    assert cat5["evidence_recall_diagnostic"] is None, "不适用不等于 0 分"

    agg = summary["recall_aggregate"]
    assert agg["applicable_records"] == 2 and agg["not_applicable_records"] == 1
    # 只在适用题上取均值：(1.0 + 0.5)/2；若把 N/A 当 0 会算成 0.5
    assert agg["mean_over_applicable_only"] == pytest.approx(0.75)


def test_runner_deterministic_flag_requests_infer_false_everywhere(stub_server, tmp_path):
    """G3b：--deterministic 必须让**每一条**写入都走 infer=false，
    并把闸门与写入通路记进 config（否则两档 digest 可能相撞）。"""
    state, base_url = stub_server
    import benchmarks.run as brun

    summary = brun.run_smoke(base_url, "locomo", top_k=5,
                             out_dir=str(tmp_path), deterministic=True)
    adds = [r["payload"] for r in state.requests if r["path"] == "/add"]
    assert adds, "没有写入请求，测试本身失效"
    assert all(p["infer"] is False for p in adds)
    assert summary["config"]["gate"] == "G3b"
    assert summary["config"]["write_path"] == "deterministic_infer_false"


def test_runner_default_smoke_stays_on_production_write_path(stub_server, tmp_path):
    """负向对照：不加 --deterministic 时一条都不许偷偷免抽取。"""
    state, base_url = stub_server
    import benchmarks.run as brun

    summary = brun.run_smoke(base_url, "locomo", top_k=5, out_dir=str(tmp_path))
    adds = [r["payload"] for r in state.requests if r["path"] == "/add"]
    assert adds and all(p["infer"] is True for p in adds)
    assert summary["config"]["gate"] == "G3a"
    assert summary["config"]["write_path"] == "production_infer_true"


def test_formal_refuses_deterministic_flag():
    """正式跑分必须与生产同路：--formal + --deterministic 在 CLI 层硬拒。"""
    import benchmarks.run as brun

    with pytest.raises(SystemExit):
        brun.main(["--formal", "--dataset", "locomo",
                   "--data-path", "/nonexistent.json", "--deterministic"])


def test_stable_digest_strips_volatile_bookkeeping_but_keeps_semantics():
    """digest 剥离必须只剥簿记字段，语义字段一个都不许丢。"""
    import benchmarks.run as brun

    a = [{"question_id": "q1", "retrieved_evidence_only": [
        {"id": "uuid-A", "hash": "h-A", "created_at": "T1",
         "memory": "同一条内容", "score": 0.9}]}]
    b = [{"question_id": "q1", "retrieved_evidence_only": [
        {"id": "uuid-B", "hash": "h-B", "created_at": "T2",
         "memory": "同一条内容", "score": 0.9}]}]
    cfg = {"mode": "smoke", "fixture_sha256": "abc"}
    assert brun._stable_digest(a, cfg) == brun._stable_digest(b, cfg)

    # 负向对照：语义内容变了，digest 必须变
    c = json.loads(json.dumps(a))
    c[0]["retrieved_evidence_only"][0]["memory"] = "内容变了"
    assert brun._stable_digest(c, cfg) != brun._stable_digest(a, cfg)
    # 语义标识（question_id / fixture_sha256）同样必须影响 digest
    d = json.loads(json.dumps(a))
    d[0]["question_id"] = "q2"
    assert brun._stable_digest(d, cfg) != brun._stable_digest(a, cfg)
    assert brun._stable_digest(a, {**cfg, "fixture_sha256": "xyz"}) \
        != brun._stable_digest(a, cfg)


def test_runner_smoke_digest_is_reproducible(stub_server, tmp_path):
    """digest 函数本身的确定性（替身服务端下 = G3b 的可控等价物）。

    注意口径：真实链路上 LLM 在环时 digest 会变，这是被测系统的性质，
    因此 G3a 只断言结构不变量、G3b 才断言 bit 相同（PROTOCOL.md §5 与
    §8 修订记录）。本测试用固定回话的替身把 LLM 排除在外，钉的是
    「输入相同 ⇒ digest 相同」这一条不该被打破的性质。
    """
    state, base_url = stub_server
    state.search_response = {"status": "ok", "results": [
        {"id": "m1", "memory": "确定性结果",
         "metadata": {"bench_session_id": "smoke_s1"}},
    ]}
    import benchmarks.run as brun

    s1 = brun.run_smoke(base_url, "longmemeval", top_k=5,
                        out_dir=str(tmp_path / "r1"))
    s2 = brun.run_smoke(base_url, "longmemeval", top_k=5,
                        out_dir=str(tmp_path / "r2"))
    assert s1["digest"] == s2["digest"], "smoke 不可复现——G3 闸门失败"
    assert s1["records_total"] == 2
    # 留证：JSONL 必须真实落盘且行数与记录数一致
    with open(s1["jsonl"], encoding="utf-8") as f:
        assert len(f.readlines()) == 2


def test_runner_smoke_digest_changes_when_results_change(stub_server, tmp_path):
    """负向对照：结果不同 ⇒ digest 必须不同（digest 不是常数）。"""
    state, base_url = stub_server
    import benchmarks.run as brun

    s1 = brun.run_smoke(base_url, "longmemeval", top_k=5,
                        out_dir=str(tmp_path / "a"))
    state.search_response = {"status": "ok", "results": [
        {"id": "m1", "memory": "变了", "metadata": {}},
    ]}
    s2 = brun.run_smoke(base_url, "longmemeval", top_k=5,
                        out_dir=str(tmp_path / "b"))
    assert s1["digest"] != s2["digest"]


def test_runner_locomo_smoke_end_to_end(stub_server, tmp_path):
    state, base_url = stub_server
    import benchmarks.run as brun

    summary = brun.run_smoke(base_url, "locomo", top_k=5,
                             out_dir=str(tmp_path))
    assert summary["records_total"] == 3  # fixture 的 3 道 QA
    assert summary["config"]["data_report"]["category_counts"] == {2: 1, 4: 1, 5: 1}


def test_formal_refuses_without_hash_manifest(tmp_path, monkeypatch):
    """哈希闸门：manifest 缺失/PENDING/哈希不符，formal 一律拒绝。"""
    import benchmarks.run as brun

    data = tmp_path / "data.json"
    data.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(brun, "MANIFEST_PATH", str(tmp_path / "no_manifest.json"))
    with pytest.raises(SystemExit, match="缺 data_manifest"):
        brun._check_formal_manifest("longmemeval", str(data))

    pending = tmp_path / "m1.json"
    pending.write_text(json.dumps({"longmemeval": {"sha256": "PENDING"}}),
                       encoding="utf-8")
    monkeypatch.setattr(brun, "MANIFEST_PATH", str(pending))
    with pytest.raises(SystemExit, match="PENDING"):
        brun._check_formal_manifest("longmemeval", str(data))

    wrong = tmp_path / "m2.json"
    wrong.write_text(json.dumps({"longmemeval": {"sha256": "0" * 64}}),
                     encoding="utf-8")
    monkeypatch.setattr(brun, "MANIFEST_PATH", str(wrong))
    with pytest.raises(SystemExit, match="哈希不匹配"):
        brun._check_formal_manifest("longmemeval", str(data))


def test_formal_refuses_without_source_commit(tmp_path, monkeypatch):
    """提交号闸门：哈希对得上但没锁上游提交号，formal 照样拒绝。

    哈希只证明「跑的是这个文件」，提交号才证明「文件取自上游哪一版」。
    两个数据集的标注都在持续修，第三方哈希对不上时没有提交号就分不清
    「取错版本」还是「文件被改过」——成绩就无法被独立复核。
    """
    import benchmarks.run as brun

    data = tmp_path / "data.json"
    data.write_text("[]", encoding="utf-8")
    real = brun._sha256_file(str(data))

    # 哈希对上、提交号缺失 → 拒
    no_commit = tmp_path / "m3.json"
    no_commit.write_text(json.dumps({"longmemeval": {"sha256": real}}),
                         encoding="utf-8")
    monkeypatch.setattr(brun, "MANIFEST_PATH", str(no_commit))
    with pytest.raises(SystemExit, match="source_commit 未锁定"):
        brun._check_formal_manifest("longmemeval", str(data))

    # 提交号还是 PENDING → 同样拒
    pending_commit = tmp_path / "m4.json"
    pending_commit.write_text(json.dumps(
        {"longmemeval": {"sha256": real, "source_commit": "PENDING"}}),
        encoding="utf-8")
    monkeypatch.setattr(brun, "MANIFEST_PATH", str(pending_commit))
    with pytest.raises(SystemExit, match="source_commit 未锁定"):
        brun._check_formal_manifest("longmemeval", str(data))

    # 正向对照：哈希 + 提交号都齐了才放行
    ok = tmp_path / "m5.json"
    ok.write_text(json.dumps(
        {"longmemeval": {"sha256": real, "source_commit": "9e0b455"}}),
        encoding="utf-8")
    monkeypatch.setattr(brun, "MANIFEST_PATH", str(ok))
    assert brun._check_formal_manifest("longmemeval", str(data))["source_commit"] \
        == "9e0b455"


def test_register_refuses_moving_or_missing_source_commit():
    """登记闸门：空/PENDING/tag 名/分支名一律拒绝，不给占位符默认值。

    给个 "unknown" 占位符最省事，也最危险——manifest 会带着一个看起来
    已登记、实则没有溯源的条目进仓库，而 formal 闸门只查「非空且非
    PENDING」，占位符正好骗得过它。
    """
    from benchmarks.download import normalize_source_commit

    for bad, why in [(None, "空"), ("", "空串"), ("  ", "全空白"),
                     ("PENDING", "占位"), ("v1.0.0", "tag 名"),
                     ("main", "分支名"), ("zzzzzzz", "非十六进制"),
                     ("abc", "不足 7 位")]:
        with pytest.raises(SystemExit):
            normalize_source_commit(bad)

    # 正向对照：短号认、长号认、大写归一成小写
    assert normalize_source_commit("9e0b455") == "9e0b455"
    assert normalize_source_commit("3EB6F2C585F5E1699204E3C3BDF7ADC5C28CB376") \
        == "3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376"


# ---------------------------------------------------------------------------
# 证据匹配器：两条召回路径形状不同，判据必须可审计（v20 修订 2）
# ---------------------------------------------------------------------------

def test_norm_text_only_collapses_whitespace_and_case():
    """归一化的边界：压空白、统一大小写，别的一概不动。"""
    import benchmarks.run as brun

    assert brun._norm_text("  学  吉他 \n") == brun._norm_text("学 吉他")
    assert brun._norm_text("Guitar") == brun._norm_text("guitar")
    # 负向对照：改写、截断、加字都不是同一串，归一化不许把它们抹平
    assert brun._norm_text("学吉他") != brun._norm_text("学习弹吉他")
    assert brun._norm_text("学吉他") != brun._norm_text("学吉")


def test_match_evidence_metadata_basis():
    """抽取路径带 metadata：按 bench_dia_id 精确回指，判据记 metadata。"""
    import benchmarks.run as brun

    results = [{"id": "m1", "memory": "英文摘要与原文完全不同",
                "metadata": {"bench_dia_id": "D1:1"}}]
    hits, basis = brun._match_evidence(results, ["D1:1", "D2:1"], {})
    assert hits == ["D1:1"]
    assert basis == {"D1:1": "metadata"}


def test_match_evidence_verbatim_basis_when_metadata_is_null():
    """verbatim 路径实测形状：``metadata`` 就是 ``None``（表里没这列）。

    只认元数据会把这种命中判成 0.0——那是假红。原文回指必须能接住，
    并且判据要如实写成 verbatim_text，让人看得出命中是怎么来的。
    """
    import benchmarks.run as brun

    results = [{"content": "  我最近开始\n 学吉他  ", "_recall_path": "like",
                "_verbatim": True, "metadata": None}]
    text_to_id = {brun._norm_text("我最近开始 学吉他"): {"D1:1"}}
    hits, basis = brun._match_evidence(results, ["D1:1"], text_to_id)
    assert hits == ["D1:1"], "首尾空白与换行属于同一串，必须能接住"
    assert basis == {"D1:1": "verbatim_text"}

    # 边界钉死：归一化只**压缩**连续空白，不**删除**空白。中文串里凭空
    # 多出一个空格是真实的字符差异，不该判为同一串——若哪天有人把
    # _norm_text "改进"成去掉全部空白，英文的词边界就会被抹掉，身份判定
    # 会退化成模糊匹配。这条测试是那次"改进"的拦路石。
    spaced = [{"content": "我最近 开始 学吉他", "metadata": None}]
    assert brun._match_evidence(spaced, ["D1:1"], text_to_id)[0] == []


def test_match_evidence_paraphrase_must_not_match():
    """关键负向对照：原文匹配是**身份判定**，不是语义给分。

    改写/翻译/摘要都不许蹭到命中——否则原文回指就成了放水通道，
    召回诊断会被自己的宽松匹配灌成虚高。
    """
    import benchmarks.run as brun

    text_to_id = {brun._norm_text("我最近开始学吉他"): {"D1:1"}}
    for paraphrase in ("我最近开始学习弹吉他",          # 改写
                       "He recently started guitar",   # 翻译
                       "学吉他",                        # 摘要/截断
                       "我最近开始学吉他了"):            # 多一个字
        results = [{"content": paraphrase, "metadata": None}]
        hits, basis = brun._match_evidence(results, ["D1:1"], text_to_id)
        assert hits == [], f"{paraphrase!r} 不该被判为命中"
        assert basis == {}


def test_match_evidence_colliding_text_records_all_ids():
    """同一段原文对应多轮（重复表述）时全记，不做任意取一。"""
    import benchmarks.run as brun

    text_to_id = {brun._norm_text("嗯"): {"D1:3", "D2:7"}}
    results = [{"content": "嗯", "metadata": None}]
    hits, basis = brun._match_evidence(results, ["D1:3", "D2:7"], text_to_id)
    assert hits == ["D1:3", "D2:7"]
    assert basis == {"D1:3": "verbatim_text", "D2:7": "verbatim_text"}


def test_match_evidence_meta_key_is_dataset_specific():
    """两个数据集的证据粒度不同：longmemeval 用会话级键，不能硬统一。"""
    import benchmarks.run as brun

    results = [{"memory": "x", "metadata": {"bench_session_id": "session_3"}}]
    hits, _ = brun._match_evidence(results, ["session_3"], {},
                                   meta_key="bench_session_id")
    assert hits == ["session_3"]
    # 拿 dia_id 的键去找会话级证据，必须找不到（键不是可互换的）
    assert brun._match_evidence(results, ["session_3"], {})[0] == []


def test_digest_ignores_model_derived_floats_but_keeps_ordering():
    """digest 新契约（修订 2）：模型派生浮点剥离，序与内容一个都不许丢。

    远程 embedding 输出实测抖到小数第 4 位，把它计入 digest 会造成
    间歇性红灯；但**只剥数值本身**——顺序、成员、正文、离散排名照旧
    进 digest，所以「检索结果变了」仍然会被抓住。
    """
    import benchmarks.run as brun

    cfg = {"mode": "smoke", "fixture_sha256": "abc"}

    def rec(score, hybrid, decay, rank=1, text="同一条内容"):
        return [{"question_id": "q1", "retrieved_evidence_only": [
            {"memory": text, "score": score, "_hybrid_score": hybrid,
             "_time_decay": decay, "_bm25_rank": rank}]}]

    base = rec(0.8123, 0.71, 0.99)
    # 浮点抖动（实测量级 ~6e-4）不许扰动 digest
    assert brun._stable_digest(rec(0.8129, 0.7104, 0.98), cfg) \
        == brun._stable_digest(base, cfg)
    # 负向对照 1：离散排名是序信息，变了必须变 digest
    assert brun._stable_digest(rec(0.8123, 0.71, 0.99, rank=2), cfg) \
        != brun._stable_digest(base, cfg)
    # 负向对照 2：正文变了必须变 digest
    assert brun._stable_digest(rec(0.8123, 0.71, 0.99, text="变了"), cfg) \
        != brun._stable_digest(base, cfg)
    # 负向对照 3：结果顺序变了必须变 digest（剥数值 ≠ 变成无序集合）
    two = [{"question_id": "q1", "retrieved_evidence_only": [
        {"memory": "甲"}, {"memory": "乙"}]}]
    flipped = [{"question_id": "q1", "retrieved_evidence_only": [
        {"memory": "乙"}, {"memory": "甲"}]}]
    assert brun._stable_digest(two, cfg) != brun._stable_digest(flipped, cfg)


# ---------------------------------------------------------------------------
# G3 闸门执行器 benchmarks/compare_runs.py
# ---------------------------------------------------------------------------

def _write_run(tmp_path, name, *, gate="G3b", digest="d" * 64, records=None,
               stats=None, data_report=None):
    """造一对可比运行的落盘留证（summary + 它指向的 JSONL）。"""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    jsonl = d / "records.jsonl"
    records = records if records is not None else [{
        "sample_id": "s1", "retrieved_count": 2, "would_answer": True,
        "evidence_recall_applicable": True, "evidence_dia_ids": ["D1:1"],
        "evidence_hits": ["D1:1"], "evidence_recall_diagnostic": 1.0,
        "retrieved_evidence_only": [{"memory": "x", "score": 0.5,
                                     "_hybrid_score": 0.4}],
    }]
    with open(jsonl, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = d / "summary.json"
    summary.write_text(json.dumps({
        "jsonl": str(jsonl),
        "digest": digest,
        "records_total": len(records),
        "config": {"dataset": "locomo", "mode": "smoke", "top_k": 5,
                   "gate": gate,
                   "write_path": ("deterministic_infer_false" if gate == "G3b"
                                  else "production_infer_true"),
                   "fixture_sha256": "f" * 64, "data_sha256": "PENDING",
                   "data_report": data_report or {"category_counts": {"2": 1}}},
        "adapter_stats": stats or {"requests": 10, "retries": 0, "timeouts": 0,
                                   "http_5xx": 0, "empty_results": 0},
    }, ensure_ascii=False), encoding="utf-8")
    return str(summary)


def test_gate_g3b_passes_on_identical_runs_and_reports_noise_floor(tmp_path):
    from benchmarks.compare_runs import compare

    a = _write_run(tmp_path, "a")
    b = _write_run(tmp_path, "b")
    rep = compare(a, b, "g3b")
    assert rep["passed"] is True and rep["failures"] == []
    assert rep["digest_equal"] is True
    assert rep["score_values_compared"] == 2
    # |Δ|=0 是最强的结果，也必须报出来——闸门要说清它量到了什么，
    # 不能只在有抖动时才开口（否则"没报"与"没量"无法区分）。
    assert rep["max_abs_delta"] == 0.0
    assert any("噪声地板" in n for n in rep["notes"]), "必须报出实测噪声地板"


def test_gate_g3b_fails_when_digest_differs(tmp_path):
    """负向对照：免抽取通路的 digest 不同 ⇒ G3b 必须红。"""
    from benchmarks.compare_runs import compare

    a = _write_run(tmp_path, "a", digest="a" * 64)
    b = _write_run(tmp_path, "b", digest="b" * 64)
    rep = compare(a, b, "g3b")
    assert rep["passed"] is False
    assert any("digest 不相同" in f for f in rep["failures"])


def test_gate_g3a_allows_digest_drift_but_still_checks_structure(tmp_path):
    """G3a 的契约：digest 允许不同（LLM 在环），结构不变量照查。"""
    from benchmarks.compare_runs import compare

    a = _write_run(tmp_path, "a", gate="G3a", digest="a" * 64)
    b = _write_run(tmp_path, "b", gate="G3a", digest="b" * 64)
    rep = compare(a, b, "g3a")
    assert rep["passed"] is True and rep["digest_equal"] is False

    # 但证据命中漂了就必须红
    drifted = [{"sample_id": "s1", "retrieved_count": 2, "would_answer": True,
                "evidence_recall_applicable": True,
                "evidence_dia_ids": ["D1:1"], "evidence_hits": [],
                "evidence_recall_diagnostic": 0.0,
                "retrieved_evidence_only": [{"memory": "x", "score": 0.5,
                                             "_hybrid_score": 0.4}]}]
    b2 = _write_run(tmp_path, "b2", gate="G3a", digest="b" * 64,
                    records=drifted)
    rep2 = compare(a, b2, "g3a")
    assert rep2["passed"] is False
    assert any("证据命中不同" in f for f in rep2["failures"])


def test_gate_rejects_impersonating_the_other_gate(tmp_path):
    """负向对照：不许拿 G3b（免抽取）的结果去顶 G3a（生产同路）通过。"""
    from benchmarks.compare_runs import compare

    a = _write_run(tmp_path, "a", gate="G3b")
    b = _write_run(tmp_path, "b", gate="G3b")
    rep = compare(a, b, "g3a")
    assert rep["passed"] is False
    assert any("不可互相顶替" in f for f in rep["failures"])


def test_gate_fails_when_every_record_is_empty(tmp_path):
    """反假绿的核心负向对照：全空运行处处相等，但不含任何信息量。"""
    from benchmarks.compare_runs import compare

    blank = [{"sample_id": "s1", "retrieved_count": 0, "would_answer": False,
              "evidence_recall_applicable": True, "evidence_dia_ids": ["D1:1"],
              "evidence_hits": [], "evidence_recall_diagnostic": 0.0,
              "retrieved_evidence_only": []}]
    a = _write_run(tmp_path, "a", records=blank)
    b = _write_run(tmp_path, "b", records=list(blank))
    rep = compare(a, b, "g3b")
    assert rep["passed"] is False
    assert any("全部" in f and "retrieved_count=0" in f for f in rep["failures"])
    assert any("结构性断裂" in f for f in rep["failures"])


def test_gate_warns_not_fails_on_partially_empty_run(tmp_path):
    """空结果是合法结果、拒答题的空更是正确行为：警告，但不判红。

    这条与上一条一起把线划清楚：全空=假绿=红；部分空=证明力弱=警告
    且写进报告。若这里判红，闸门会因数据难度长红，等于没有闸门。
    """
    from benchmarks.compare_runs import compare

    mixed = [
        {"sample_id": "hit", "retrieved_count": 2, "would_answer": True,
         "evidence_recall_applicable": True, "evidence_dia_ids": ["D1:1"],
         "evidence_hits": ["D1:1"], "evidence_recall_diagnostic": 1.0,
         "retrieved_evidence_only": [{"memory": "x", "score": 0.5}]},
        {"sample_id": "abstain", "retrieved_count": 0, "would_answer": False,
         "evidence_recall_applicable": False, "evidence_dia_ids": [],
         "evidence_hits": [], "evidence_recall_diagnostic": None,
         "retrieved_evidence_only": []},
    ]
    a = _write_run(tmp_path, "a", records=mixed)
    b = _write_run(tmp_path, "b", records=json.loads(json.dumps(mixed)))
    rep = compare(a, b, "g3b")
    assert rep["passed"] is True, rep["failures"]
    assert any("1/2 条记录有实质检索结果" in w for w in rep["warnings"])
    assert any("拒答题" in w for w in rep["warnings"])


def test_gate_catches_swallowed_timeouts(tmp_path):
    """历史假绿的真凶：run B 吞了超时，digest 却没动。失败分类不许漂。"""
    from benchmarks.compare_runs import compare

    a = _write_run(tmp_path, "a",
                   stats={"requests": 10, "retries": 0, "timeouts": 0})
    b = _write_run(tmp_path, "b",
                   stats={"requests": 13, "retries": 3, "timeouts": 3})
    rep = compare(a, b, "g3b")
    assert rep["passed"] is False
    assert any("adapter_stats.timeouts 漂移" in f for f in rep["failures"])
    # requests/retries 本身是环境噪声，不该单独判红
    assert not any("adapter_stats.requests" in f for f in rep["failures"])


def test_gate_score_tolerance_fails_beyond_threshold(tmp_path):
    """容差是显式的：抖动在容差内放过，超出必须红并报出实测量级。"""
    from benchmarks.compare_runs import compare

    def rec(score):
        return [{"sample_id": "s1", "retrieved_count": 1, "would_answer": True,
                 "evidence_recall_applicable": True,
                 "evidence_dia_ids": ["D1:1"], "evidence_hits": ["D1:1"],
                 "evidence_recall_diagnostic": 1.0,
                 "retrieved_evidence_only": [{"memory": "x", "score": score}]}]

    a = _write_run(tmp_path, "a", records=rec(0.500000))
    near = _write_run(tmp_path, "near", records=rec(0.500600))   # 6e-4，实测量级
    far = _write_run(tmp_path, "far", records=rec(0.560000))     # 6e-2，远超

    ok = compare(a, near, "g3b")
    assert ok["passed"] is True
    assert ok["max_abs_delta"] == pytest.approx(6e-4, rel=1e-3)

    bad = compare(a, far, "g3b")
    assert bad["passed"] is False
    assert any("超出容差" in f for f in bad["failures"])


def test_gate_detects_structure_change_not_as_numeric_jitter(tmp_path):
    """检索条数变了是结构变化，不许被当成"数值抖动"糊过去。"""
    from benchmarks.compare_runs import compare

    base = {"sample_id": "s1", "retrieved_count": 2, "would_answer": True,
            "evidence_recall_applicable": True, "evidence_dia_ids": ["D1:1"],
            "evidence_hits": ["D1:1"], "evidence_recall_diagnostic": 1.0}
    a = _write_run(tmp_path, "a", records=[dict(
        base, retrieved_evidence_only=[{"memory": "x", "score": 0.5},
                                       {"memory": "y", "score": 0.4}])])
    b = _write_run(tmp_path, "b", records=[dict(
        base, retrieved_evidence_only=[{"memory": "x", "score": 0.5}])])
    rep = compare(a, b, "g3b")
    assert rep["passed"] is False
    assert any("可比数值个数不同" in f for f in rep["failures"])


def test_gate_cli_exit_codes(tmp_path, capsys):
    """退出码可判：0 通过 / 1 断言失败 / 2 用法或文件问题。"""
    from benchmarks.compare_runs import main

    a = _write_run(tmp_path, "a")
    b = _write_run(tmp_path, "b")
    assert main([a, b, "--gate", "g3b"]) == 0
    bad = _write_run(tmp_path, "bad", digest="0" * 64)
    assert main([a, bad, "--gate", "g3b"]) == 1
    assert main([a, str(tmp_path / "nope.json"), "--gate", "g3b"]) == 2


# ---------------------------------------------------------------------------
# 修正清单：版本化、钉哈希、只许重述标注、不许静默失效（PROTOCOL.md §1.1）
# ---------------------------------------------------------------------------

def _corr_sample(**over):
    """复用上面的 LoCoMo 夹具，补一道 cat5 拒答题当 mark_adversarial 的靶子。

    刻意**不**新写一份样本：两份同名夹具会互相遮蔽，前面的 schema 测试就会
    悄悄换掉输入。qa 下标固定为 0=常规题（带 evidence）、1=cat5 拒答题。
    """
    return _locomo_sample(qa=[
        {"question": "谁先打招呼？", "answer": "A", "category": 4,
         "evidence": ["D1:1"]},
        {"question": "A 的生日是哪天", "adversarial_answer": "没提过",
         "category": 5},
    ], **over)


def _write_corr(path, **kw):
    body = {"schema_version": 1, "dataset": "locomo",
            "manifest_version": "t-v1", "corrections": []}
    body.update(kw)
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_shipped_correction_manifest_is_empty_and_moves_nothing():
    """随仓库发布的清单是空清单：能装载、改不动任何数字。

    空清单**不要求**钉数据哈希——它改不动数字，钉了没有意义。这道门槛
    正好落在「能改动数字」的那一刻，下一个测试钉的就是那一刻。
    """
    import os

    from benchmarks.corrections import apply_corrections, load_corrections

    shipped = os.path.join("benchmarks", "corrections", "locomo_v0.json")
    loaded = load_corrections(shipped, dataset="locomo", data_sha256="0" * 64)
    assert loaded["manifest_version"] == "locomo-v0"
    assert loaded["count"] == 0
    # 空清单即使钉的是 PENDING 也放行（对照：下一个测试里非空就拒）
    assert loaded["applies_to_sha256"] == "PENDING"

    data = [_corr_sample()]
    out, report = apply_corrections("locomo", data, loaded)
    assert report["applied"] == 0 and report["details"] == []
    assert out == data


def test_nonempty_correction_manifest_must_pin_the_data_hash(tmp_path):
    """能改动数字的清单必须钉住它改的是哪份数据。"""
    from benchmarks.corrections import CorrectionError, load_corrections

    op = {"op": "mark_adversarial", "sample_index": 0, "qa_index": 1,
          "why": "官方口径此题为拒答题"}

    # 非空 + 没钉哈希 → 拒
    p = _write_corr(tmp_path / "c1.json", corrections=[op])
    with pytest.raises(CorrectionError, match="没钉数据哈希"):
        load_corrections(p, dataset="locomo", data_sha256="a" * 64)

    # 非空 + 钉了 PENDING → 同样拒
    p = _write_corr(tmp_path / "c2.json", corrections=[op],
                    applies_to_sha256="PENDING")
    with pytest.raises(CorrectionError, match="没钉数据哈希"):
        load_corrections(p, dataset="locomo", data_sha256="a" * 64)

    # 非空 + 钉了别份数据的哈希 → 拒（拿 A 的修正改 B 的分数）
    p = _write_corr(tmp_path / "c3.json", corrections=[op],
                    applies_to_sha256="b" * 64)
    with pytest.raises(CorrectionError, match="不予放行"):
        load_corrections(p, dataset="locomo", data_sha256="a" * 64)

    # 正向对照：钉对了才放行
    p = _write_corr(tmp_path / "c4.json", corrections=[op],
                    applies_to_sha256="a" * 64)
    assert load_corrections(p, dataset="locomo", data_sha256="a" * 64)["count"] == 1


def test_correction_manifest_rejects_illegal_shapes(tmp_path):
    """清单本身的合法性：版本号、数据集、动作白名单、未知键、理由。"""
    from benchmarks.corrections import CorrectionError, load_corrections

    ok_op = {"op": "mark_adversarial", "sample_index": 0, "qa_index": 1,
             "why": "官方口径此题为拒答题"}
    pin = {"applies_to_sha256": "a" * 64}

    cases = [
        ("schema_version", {"schema_version": 99}, "schema_version"),
        ("no_version", {"manifest_version": ""}, "manifest_version"),
        ("other_dataset", {"dataset": "longmemeval"}, "本次跑的是"),
        ("not_a_list", {"corrections": {}}, "必须是列表"),
        ("unknown_op", dict(pin, corrections=[dict(ok_op, op="delete_qa")]),
         "不在白名单"),
        ("typo_key", dict(pin, corrections=[dict(ok_op, whyy="拼错了")]),
         "未知键"),
        ("no_why", dict(pin, corrections=[
            {"op": "mark_adversarial", "sample_index": 0, "qa_index": 1}]),
         "缺 why"),
        ("neg_index", dict(pin, corrections=[dict(ok_op, sample_index=-1)]),
         "必须是非负整数"),
        ("empty_dia", dict(pin, corrections=[
            {"op": "add_evidence", "sample_index": 0, "qa_index": 0,
             "dia_ids": [], "why": "上游漏标"}]),
         "非空字符串列表"),
    ]
    for name, patch, expect in cases:
        p = _write_corr(tmp_path / f"bad_{name}.json", **patch)
        with pytest.raises(CorrectionError, match=expect):
            load_corrections(p, dataset="locomo", data_sha256="a" * 64)


def test_correction_manifest_refuses_to_rewrite_answers(tmp_path):
    """负向对照（红线）：改答案正文不是修正，是造数据——schema 层拦死。"""
    from benchmarks.corrections import CorrectionError, load_corrections

    for field in ("answer", "question", "adversarial_answer", "text"):
        op = {"op": "mark_adversarial", "sample_index": 0, "qa_index": 1,
              "why": "想顺手改正文", field: "我说的才对"}
        p = _write_corr(tmp_path / f"rw_{field}.json", corrections=[op],
                        applies_to_sha256="a" * 64)
        with pytest.raises(CorrectionError, match="造数据"):
            load_corrections(p, dataset="locomo", data_sha256="a" * 64)


def test_corrections_apply_on_a_copy_and_are_itemized(tmp_path):
    """正向：补 evidence / 标拒答都生效，且**原数据一个字节不动**。"""
    from benchmarks.corrections import apply_corrections, load_corrections

    p = _write_corr(
        tmp_path / "good.json",
        applies_to_sha256="a" * 64,
        corrections=[
            {"op": "add_evidence", "sample_index": 0, "qa_index": 0,
             "dia_ids": ["D1:2"], "why": "上游漏标了乙的追问这一轮"},
            {"op": "mark_adversarial", "sample_index": 0, "qa_index": 1,
             "why": "官方口径此题为拒答题"},
        ])
    loaded = load_corrections(p, dataset="locomo", data_sha256="a" * 64)

    data = [_corr_sample()]
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    out, report = apply_corrections("locomo", data, loaded)

    assert report["applied"] == 2
    assert out[0]["qa"][0]["evidence"] == ["D1:1", "D1:2"]
    assert out[0]["qa"][1]["_marked_adversarial"] is True
    # 逐条留痕：改了哪道题、加了什么、为什么
    assert report["details"][0]["added"] == ["D1:2"]
    assert "漏标" in report["details"][0]["why"]
    # 原对象没被就地改写（磁盘原件更不会动）
    assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before


def test_stale_correction_is_an_error_not_a_silent_noop(tmp_path):
    """匹配不到目标的修正必须报错——静默跳过会让清单腐烂、让报告说谎。"""
    from benchmarks.corrections import (CorrectionError, apply_corrections,
                                        load_corrections)

    def loaded_with(op):
        p = _write_corr(tmp_path / f"stale_{op['op']}_{op['qa_index']}.json",
                        applies_to_sha256="a" * 64, corrections=[op])
        return load_corrections(p, dataset="locomo", data_sha256="a" * 64)

    data = [_corr_sample()]

    # 下标越界
    with pytest.raises(CorrectionError, match="只有 2 道题"):
        apply_corrections("locomo", data, loaded_with(
            {"op": "mark_adversarial", "sample_index": 0, "qa_index": 9,
             "why": "越界"}))
    with pytest.raises(CorrectionError, match="个样本"):
        apply_corrections("locomo", data, loaded_with(
            {"op": "mark_adversarial", "sample_index": 7, "qa_index": 0,
             "why": "越界"}))

    # dia_id 在该样本里根本不存在——补一个不存在的证据只会凭空拉高召回
    with pytest.raises(CorrectionError, match="凭空拉高召回"):
        apply_corrections("locomo", data, loaded_with(
            {"op": "add_evidence", "sample_index": 0, "qa_index": 0,
             "dia_ids": ["D9:9"], "why": "查无此轮"}))

    # 要补的 evidence 已经在题里了——这条修正已失效，必须报错让人删掉
    with pytest.raises(CorrectionError, match="已经全在题里"):
        apply_corrections("locomo", data, loaded_with(
            {"op": "add_evidence", "sample_index": 0, "qa_index": 0,
             "dia_ids": ["D1:1"], "why": "重复"}))

    # 官方口径只有 cat5 走拒答判定，别的类别不许标
    with pytest.raises(CorrectionError, match="只有 cat5"):
        apply_corrections("locomo", data, loaded_with(
            {"op": "mark_adversarial", "sample_index": 0, "qa_index": 0,
             "why": "类别不符"}))


def test_marked_adversarial_actually_removes_the_question_from_recall(stub_server):
    """标记必须**真的被消费**：拒答题从召回分母里拿掉，且逐条留痕。

    一个没人读的标记等于没有这个机制。这里钉的是 run_locomo_sample
    确实认这个标记——否则 mark_adversarial 只是一句空承诺。
    """
    import benchmarks.run as brun

    state, base_url = stub_server
    state.search_response = {"status": "ok", "results": [
        {"id": "m1", "memory": "你好",
         "metadata": {"bench_dia_id": "D1:1", "bench_session_id": "session_1"}},
    ]}
    adapter = _adapter(base_url)

    # 两道题都配上 evidence，好让"被拿掉"这件事看得见：
    # 未标记时该题召回 1.0，标记后整题退出分母（None）。
    sample = _corr_sample()
    sample["qa"][1]["evidence"] = ["D1:1"]

    plain = brun.run_locomo_sample(adapter, json.loads(json.dumps(sample)), top_k=3)
    assert plain[1]["evidence_recall_applicable"] is True
    assert plain[1]["evidence_recall_diagnostic"] == 1.0
    assert plain[1]["correction_marked_adversarial"] is False

    marked = json.loads(json.dumps(sample))
    marked["qa"][1]["_marked_adversarial"] = True
    after = brun.run_locomo_sample(adapter, marked, top_k=3)
    assert after[1]["evidence_recall_applicable"] is False
    assert after[1]["evidence_recall_diagnostic"] is None
    assert after[1]["correction_marked_adversarial"] is True
    # 正向对照：另一道题不受影响，标记只作用于被点名那一题
    assert after[0]["evidence_recall_applicable"] is True
    assert after[0]["correction_marked_adversarial"] is False
    # 检索确实发生过（不是因为没跑到而"看起来"通过）
    assert sum(1 for r in state.requests if r["path"] == "/search") == 4


def test_corrections_cannot_be_applied_invisibly():
    """修正块进 config，config 进 digest——不存在「悄悄改了分数」的路径。"""
    import benchmarks.run as brun

    records = [{"sample_id": "s1", "evidence_hits": ["D1:1"]}]
    base = {"mode": "formal", "dataset": "locomo",
            "corrections": {"manifest_version": None, "applied": 0,
                            "details": []}}
    with_corr = {"mode": "formal", "dataset": "locomo",
                 "corrections": {"manifest_version": "t-v1", "applied": 1,
                                 "details": [{"op": "mark_adversarial",
                                              "at": [0, 1], "why": "x"}]}}
    assert brun._stable_digest(records, base) \
        != brun._stable_digest(records, with_corr)
    # 正向对照：同样的配置两遍必须一致（digest 只对内容敏感）
    assert brun._stable_digest(records, base) \
        == brun._stable_digest(records, json.loads(json.dumps(base)))


def test_formal_demands_a_zero_correction_baseline(tmp_path):
    """非空修正要跑正式成绩，必须拿同一份数据的零修正基线做对照。"""
    import benchmarks.run as brun

    entry = {"sha256": "a" * 64, "source_commit": "9e0b455"}
    p = _write_corr(tmp_path / "one.json", applies_to_sha256="a" * 64,
                    corrections=[{"op": "mark_adversarial", "sample_index": 0,
                                  "qa_index": 1, "why": "官方口径为拒答题"}])

    # 没给基线 → 拒
    with pytest.raises(SystemExit, match="sensitivity-baseline"):
        brun._load_formal_corrections("locomo", p, entry, None)

    # 基线文件不存在 → 拒
    with pytest.raises(SystemExit, match="基线 summary 不存在"):
        brun._load_formal_corrections("locomo", p, entry,
                                      str(tmp_path / "nope.json"))

    def baseline(name, **cfg):
        f = tmp_path / name
        body = {"digest": "d" * 64, "recall_aggregate": {},
                "config": dict({"dataset": "locomo", "data_sha256": "a" * 64,
                                "corrections": {"applied": 0}}, **cfg)}
        f.write_text(json.dumps(body), encoding="utf-8")
        return str(f)

    # 基线是别的数据集 → 拒
    with pytest.raises(SystemExit, match="本次跑"):
        brun._load_formal_corrections(
            "locomo", p, entry, baseline("b1.json", dataset="longmemeval"))

    # 基线跑的不是同一份数据 → 拒（那叫换了题再比分）
    with pytest.raises(SystemExit, match="换了题再比分"):
        brun._load_formal_corrections(
            "locomo", p, entry, baseline("b2.json", data_sha256="b" * 64))

    # 基线自己带着修正 → 拒（比不出修正的影响）
    with pytest.raises(SystemExit, match="基线必须是零修正"):
        brun._load_formal_corrections(
            "locomo", p, entry, baseline("b3.json",
                                         corrections={"applied": 2}))

    # 正向对照：合格基线放行，并把基线信息带进 config 留证
    loaded, base = brun._load_formal_corrections(
        "locomo", p, entry, baseline("b4.json"))
    assert loaded["count"] == 1
    assert base["path"] == "b4.json" and base["digest"] == "d" * 64

    # 零修正运行不需要基线（也不该被逼着给一个）
    empty, none_base = brun._load_formal_corrections("locomo", None, entry, None)
    assert empty["count"] == 0 and none_base is None


def test_correction_ops_are_documented_in_protocol():
    """代码里加了新动作，协议里必须同步写明——文档漂移守卫。"""
    from benchmarks.corrections import ALLOWED_OPS

    with open("benchmarks/PROTOCOL.md", encoding="utf-8") as f:
        protocol = f.read()

    assert "### 1.1 修正清单" in protocol
    for op in ALLOWED_OPS:
        assert f"`{op}`" in protocol, f"新动作 {op} 未写进 PROTOCOL.md §1.1"
    # 随仓库发布的空清单必须在协议里点名，否则读者无从知道它存在
    assert "benchmarks/corrections/locomo_v0.json" in protocol
    # 敏感性分析与哈希钉这两条硬约束必须留在纸面上
    assert "--sensitivity-baseline" in protocol
    assert "applies_to_sha256" in protocol

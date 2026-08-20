"""v20 评测管线（benchmarks/）契约测试。

三层钉死：

1. schema 校验器——合法 fixture 通过；重复 question_id / 未知题型 /
   证据越界 / dia_id 重复必须拒绝；LoCoMo 上游已知标注问题进 anomalies
   而不是被静默修掉。
2. 适配器——对一个**线程内真实 HTTP 服务**（http.server）跑完整 urllib
   栈：同步写、异步回执必须等 job、/search 的 HTTP 200 + body error 必须
   抛错（组件故障 ≠ 空结果，这是负向对照）、5xx 有限重试后失败且计数、
   空结果诚实计数、X-Request-ID 随请求出门、delete_all 带 confirm。
3. runner——smoke 两次运行 digest 一致（G3 复现性）；LongMemEval 注入
   强制排除 question_date 之后的会话（防泄漏）；未检索到证据时
   would_answer=False（未检索不作答）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from benchmarks.adapter import (
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
            "payload": payload,
        })

    def do_GET(self):
        st = self.state
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
            return self._reply(200, {"status": "ok", "action": "direct"})
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


def test_runner_smoke_digest_is_reproducible(stub_server, tmp_path):
    """G3 复现性闸门：同一配置跑两遍，digest 必须一致。"""
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

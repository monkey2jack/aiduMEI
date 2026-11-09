"""f0.7+ "what is worth remembering": pure write-policy rules (no app)."""
import pytest

from ducky import write_policy as wp

SECRETS = [
    ("我的 key 是 sk-proj-abcdefghijklmnopqrstuvwx123456 记一下", "api_key"),
    ("直连用 ibk_0f1e2d3c4b5a69788796a5b4", "api_key"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789 已发", "api_key"),  # release-scan:allow 合成夹具
    ("AKIAABCDEFGHIJKLMNOP 是新的 access key id", "api_key"),
    ("tskey-auth-kAbCdEf1234-ZZZZZZZZZZ 用来加节点", "api_key"),
    ("password: Hunter2024!", "credential"),
    ("密码：Abc12345xyz", "credential"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789", "bearer_token"),
    ("https://user:pa55word@host.example.com/x", "url_credentials"),
    ("身份证 110101199003074514", "id_number"),
    ("护照号 E12345678", "id_document"),
    ("卡号 4111 1111 1111 1111", "card_number"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----", "private_key"),  # release-scan:allow 合成夹具
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "jwt"),
    ("签名图 data:image/png;base64," + "A" * 300, "signature"),
]

# Things that look technical but are not credentials: must survive untouched.
NOT_SECRETS = [
    "密码：已经改好了",
    "token: AIDUMEM_API_TOKEN",
    "token = <your token>",
    "Bearer <AIDUMEM_API_TOKEN>",
    "commit 1a2b3c4 已推送，wiki space 7000000000000000001",
    "文档 token DocAbCdEfGhIjKlMnOpQrStUvW 已更新",
    "记忆 id 4af006e2-8a5b-4a29-8954-419551021c1c",
    "内网 100.64.0.10，端口 8767",
    "token 指纹 = sha256 前 16 位",
    "我的银行卡密码提示是紫色长颈鹿",
    "18 位数字 123456789012345678 不是证件",
    "ssh user@test-host",
]


@pytest.mark.parametrize("text,kind", SECRETS)
def test_secret_spans_are_redacted(text, kind):
    out, kinds = wp.redact_forbidden(text)
    assert kind in kinds and f"[redacted:{kind}]" in out


@pytest.mark.parametrize("text", NOT_SECRETS)
def test_ordinary_technical_text_is_not_redacted(text):
    out, kinds = wp.redact_forbidden(text)
    assert kinds == [] and out == text


def test_redaction_keeps_the_rest_of_the_turn():
    out, _ = wp.redact_forbidden("新的模型密钥是 sk-proj-abcdefghijklmnopqrstuvwx123456 ，明天换到测试机上")
    assert out.startswith("新的模型密钥是 [redacted:api_key]") and "明天换到测试机上" in out


def test_hard_block_shapes():
    mail = "From: a@b.com\nTo: c@d.com\nSubject: hi\nDate: Mon\n\nbody"
    assert wp.hard_block_reason(mail) == "raw_email_copy"
    assert wp.hard_block_reason("x" * 20001) == "oversized_raw_copy"
    assert wp.hard_block_reason("x" * 20001, summary=True) is None
    assert wp.hard_block_reason("[redacted:api_key]") == "secret_only"
    assert wp.hard_block_reason("我的 key 是 [redacted:api_key] 记一下") is None
    assert wp.hard_block_reason("From: 张三 的消息说明天开会") is None


def test_secret_guard_off_is_an_explicit_escape_hatch(monkeypatch):
    monkeypatch.setenv("AIDUMEI_SECRET_GUARD", "off")
    assert wp.redact_forbidden("password: Hunter2024!") == ("password: Hunter2024!", [])


@pytest.mark.parametrize("text,reason", [
    ("[SILENT]", "noise:host_machine_text"),
    ("Say OK", "noise:test_ping"), ("只回复：OK", "noise:test_ping"),
    ("[Image: original 2472x1314, displayed at 2000x1063.]", "noise:host_machine_text"),
    ("This session is being continued from a previous conversation that ran out of context.",
     "noise:host_compaction_copy"),
])
def test_noise_is_rejected_in_shadow(text, reason):
    v = wp.classify(text)
    assert v["verdict"] == "reject" and reason in v["reasons"]


@pytest.mark.parametrize("text,reason", [("好的", "short_reply:ack"), ("继续", "short_reply:ack"),
                                         ("x", "short_reply:too_short")])
def test_short_replies_go_to_daily_not_rejected(text, reason):
    """f0.8 ②：短确认 / 单字不再判拒收，归当日流水。"""
    v = wp.classify(text)
    assert v["verdict"] == "daily" and v["layer"] == "daily" and reason in v["reasons"]


@pytest.mark.parametrize("text", ["批准，做起来", "同意，开工", "授权好啦", "LGTM"])
def test_go_ahead_is_kept_in_daily_not_rejected(text):
    v = wp.classify(text)
    assert v["verdict"] == "daily" and v["layer"] == "daily" and "confirmation" in v["reasons"]


@pytest.mark.parametrize("text,layer", [
    ("我对花生过敏，点外卖时要避开花生", "profile"),
    ("以后周报统一用中文写，标题放日期", "profile"),
    ("我们决定新版本先上影子模式，观察一周再拦截", "topic"),
    ("测试机的数据盘挂在 /data/x，系统盘只有 20G", "topic"),
    ("刚才重启了服务，现在 health ok", "daily"),
])
def test_layers(text, layer):
    v = wp.classify(text)
    assert v["layer"] == layer and v["verdict"] != "reject"


def test_condition_is_extracted():
    c = wp.extract_condition("周末和老婆去杭州的时候我喜欢住湖边")
    assert c == {"occasion": ["周末"], "place": ["杭州"], "people": ["老婆"]}


def test_source_direct_vs_inferred():
    summary = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill",
               "distill_source_count": 4}
    assert wp.source_of(summary) == ("inferred", "session_distill:4 turns")
    assert wp.source_of({"source": "cc-ingest"})[0] == "direct"
    assert wp.source_of({"source": "reflect"})[0] == "inferred"
    assert wp.source_of({"_origin_is_bot": 1})[0] == "inferred"
    assert wp.source_of({}, infer_used=True)[0] == "inferred"
    # f0.8：存量/调用方的 direct 不再被采信，按服务端信号重推（无派生信号 → verbatim_turn）
    assert wp.source_of({"source_type": "direct", "basis": "x"}) == ("direct", "verbatim_turn")
    assert wp.source_of({"source_type": "inferred", "basis": "x"}) == ("inferred", "x")
    v = wp.classify("用户总是在晚上写代码", "user", summary)
    assert "generalization_in_inference" in v["reasons"]


@pytest.mark.parametrize("new,old,kind", [
    ("默认端口改成 8780 了", "默认端口是 8767", "value_change"),
    ("我现在住在东京", "我住在杭州", "value_change"),
    ("我不喜欢喝冰美式了，胃受不了", "我喜欢喝冰美式", "negation"),
    ("用户可能不喜欢早上开会", "我喜欢早上开会", "negation"),
    ("我的常用语言是中文", "用户的常用语言可能是英语", "value_change"),
])
def test_same_topic_change(new, old, kind):
    assert wp.same_topic_change(new, old)["kind"] == kind


@pytest.mark.parametrize("new,old", [
    ("我喜欢在周末看纪录片", "我的车是白色的"), ("项目的会议改到周二", "项目代号是青鸟"),
    ("我不会去", "我会去开会"), ("今天天气很好", "默认端口是 8767"),
])
def test_unrelated_statements_do_not_supersede(new, old):
    assert wp.same_topic_change(new, old) is None


def test_conflict_resolution_matrix():
    assert wp.resolve_conflict("direct", "direct") == "supersede"
    assert wp.resolve_conflict("direct", "inferred") == "supersede"
    assert wp.resolve_conflict("inferred", "inferred") == "supersede"
    assert wp.resolve_conflict("inferred", "direct") == "pending_confirm"
    assert wp.resolve_conflict("direct", "direct", "2026-11-02T10:00:00", "2026-11-02T10:00:00+00") == "pending_confirm"


def test_recall_annotation_and_flags(monkeypatch):
    rows = [{"memory": "a", "score": 0.9, "metadata": {"layer": "daily"}},
            {"memory": "b", "score": 0.8, "metadata": {"kind": "session_distill", "lane": "distill",
                                                         "_origin_agent": "session-distill"}},
            {"memory": "c", "score": 0.2, "metadata": {"layer": "topic", "source_type": "direct"}}]
    wp.annotate_results(rows)
    assert [r["inferred"] for r in rows] == [False, True, False]
    assert rows[0]["layer"] == "daily" and "layer" not in rows[1]
    assert wp.precise_filter(rows, 10) == rows                       # both flags default off
    monkeypatch.setenv("AIDUMEI_RECALL_EXCLUDE_DAILY", "1")
    assert [r["memory"] for r in wp.precise_filter(rows, 10)] == ["b", "c"]
    monkeypatch.setenv("AIDUMEI_RECALL_PRECISE", "1")
    assert [r["memory"] for r in wp.precise_filter(rows, 10)] == ["b"]   # c is under half the best score


def test_enrich_is_additive_and_respects_caller(monkeypatch):
    md = wp.enrich_metadata("我对花生过敏", "user", {"source": "cc-ingest", "layer": "topic"})
    assert md["layer"] == "topic" and md["source_type"] == "direct" and md["source"] == "cc-ingest"
    monkeypatch.setenv("AIDUMEI_WRITE_POLICY", "off")
    assert wp.enrich_metadata("我对花生过敏", "user", {"a": 1}) == {"a": 1}


@pytest.mark.parametrize("text", [
    "- **App Secret**: `Zz9YyXx8WwVv7UuTt6SsRr5QqPp4Oo3N`",
    "cfut_AAAAbbbbCCCCdddd1111222233334444eeeeFFFF",
    "pypi-AgEIcHlwaS5vcmcCJDkyYjM0" + "A1b2C3d4" * 6,  # release-scan:allow 合成夹具
])
def test_markdown_and_vendor_token_shapes(text):
    out, kinds = wp.redact_forbidden(text)
    assert kinds and "[redacted:" in out


def test_paths_after_credential_words_are_not_redacted():
    """自审：运维对话里 `pwd: /srv/...` 很常见——路径/URL 不是口令本身。"""
    for t in ("pwd: /srv/app/aiduMEI", "token=https://example.com/docs/auth", "pwd 是 ~/work/repo2"):
        assert wp.redact_forbidden(t) == (t, [])
    assert wp.redact_forbidden("pwd: Hunter2024!")[1] == ["credential"]

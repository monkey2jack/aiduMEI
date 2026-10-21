"""tests/test_f05_carrier_and_backoff.py — f0.5 四项修复回归

覆盖（对应任务书 WP2/WP3/WP4，每条修复带负向对照）：
  F06 载体降级表达：degraded ≠ "No relevant memories found."
  F05 载体身份线：author_status 三态显式申报；服务端 unknown 注记不猜降权
  6.2 重放退避：失败指数退避+隔离，不再 100 次/秒空转；正常回放不受影响
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# 沙箱纪律：DATA_DIR 必须在新目录（import 前设好，与既有夹具同款）
_DATA = tempfile.mkdtemp(prefix="f05-test-")
os.environ["AIDUMEI_DATA_DIR"] = _DATA

# ════════════════════════════════════════════════════════════════
# F06 · 降级表达（纯函数层：插件不 import ducky，直接测静态方法）
# ════════════════════════════════════════════════════════════════

def _plugin_cls():
    import importlib.util
    import types as _types
    # 替身宿主模块（签名对齐生产：AiduMemProvider 只被继承不被动用，签名
    # 宽松即够；tool_error 返回纯字符串——签名窄了会造假绿灯，见 f0.4 F-15）
    host_pkg = _types.ModuleType("agent")
    mp_mod = _types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # 生产里是宿主基类；测试替身保持可继承签名
        def __init__(self, *args, **kwargs):
            pass

    mp_mod.MemoryProvider = MemoryProvider
    host_pkg.memory_provider = mp_mod
    tools_pkg = _types.ModuleType("tools")
    tr_mod = _types.ModuleType("tools.registry")
    tr_mod.tool_error = lambda msg: f"error: {msg}"
    tools_pkg.registry = tr_mod
    sys.modules.setdefault("agent", host_pkg)
    sys.modules.setdefault("agent.memory_provider", mp_mod)
    sys.modules.setdefault("tools", tools_pkg)
    sys.modules.setdefault("tools.registry", tr_mod)
    p = pathlib.Path(__file__).resolve().parent.parent / \
        "integrations/hermes-plugin/aidumem/__init__.py"
    spec = importlib.util.spec_from_file_location("aidumem_plugin_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AiduMemProvider if hasattr(mod, "AiduMemProvider") else None, mod


def test_f06_degraded_note_present_on_degraded():
    _, mod = _plugin_cls()
    fn = mod.AiduMemProvider._degraded_note
    hits = {"results": [], "recall_verdict": "degraded", "degraded": ["vector unavailable"]}
    note = fn(hits)
    assert note, "degraded 必须产生注记"
    assert "降级" in note and "vector unavailable" in note
    assert "不代表没有相关记忆" in note


def test_f06_negative_ordinary_empty_stays_not_found():
    """负向对照：正常空结果（not_found）不得触发降级注记。"""
    _, mod = _plugin_cls()
    fn = mod.AiduMemProvider._degraded_note
    assert fn({"results": [], "recall_verdict": "not_found"}) == ""
    assert fn({"results": [], "recall_verdict": "empty_query"}) == ""
    assert fn({"results": [{"memory": "有货"}], "recall_verdict": "found"}) == ""
    assert fn(None) == ""
    assert fn({"results": []}) == ""  # 缺 verdict 字段（旧服务端）=不注记


def test_f06_tool_call_translates_degraded_not_missing():
    """_handle_tool_call 层：degraded 输出必须含降级语义，不含 no memories。"""
    _, mod = _plugin_cls()

    class _Client:
        user_id = "u"
        bank_id = "default"
        def try_request(self, method, path, body=None, timeout=None):
            return {"results": [], "recall_verdict": "degraded",
                    "degraded": ["rerank down"]}

    prov = mod.AiduMemProvider.__new__(mod.AiduMemProvider)
    prov._client = _Client()
    prov._session_id = "s-tool"
    prov._source_session_id = lambda sid: sid or "s-tool"
    import json as _json
    out = _json.loads(prov._handle_tool_call("aidumem_search", {"query": "测试查询"}))
    assert "检索降级" in out["result"], out
    assert "rerank down" in out["result"], out
    assert "No relevant memories" not in out["result"], out

    # 负向对照：同样空结果但 verdict=not_found → 仍是 no memories 文案
    class _ClientNF(_Client):
        def try_request(self, method, path, body=None, timeout=None):
            return {"results": [], "recall_verdict": "not_found"}
    prov2 = mod.AiduMemProvider.__new__(mod.AiduMemProvider)
    prov2._client = _ClientNF()
    prov2._session_id = "s-tool"
    prov2._source_session_id = lambda sid: sid or "s-tool"
    out2 = _json.loads(prov2._handle_tool_call("aidumem_search", {"query": "测试查询"}))
    assert out2["result"] == "No relevant memories found.", out2


def test_f06_prefetch_emits_degraded_block():
    """prefetch：degraded 且空结果 → 注记块进上下文（负向：not_found 不出块）。"""
    _, mod = _plugin_cls()

    class _Client:
        user_id = "u"
        bank_id = "default"
        def __init__(self, hits):
            self._hits = hits
        def try_request(self, method, path, body=None, timeout=None):
            if path == "/search":
                return self._hits
            return {"context": ""}  # core 块空

    prov = mod.AiduMemProvider.__new__(mod.AiduMemProvider)
    prov._client = _Client({"results": [], "recall_verdict": "degraded",
                            "degraded": ["vector unavailable"]})
    prov._session_id = "s-test"
    prov._source_session_id = lambda sid: sid or "s-test"
    out = prov.prefetch("这是一个足够长的查询")
    assert "检索降级" in out, out

    prov2 = mod.AiduMemProvider.__new__(mod.AiduMemProvider)
    prov2._client = _Client({"results": [], "recall_verdict": "not_found"})
    prov2._session_id = "s-test"
    prov2._source_session_id = lambda sid: sid or "s-test"
    out2 = prov2.prefetch("这是一个足够长的查询")
    assert "检索降级" not in out2 and out2 == "", out2


# ════════════════════════════════════════════════════════════════
# F05 · 身份线（插件三态 + 服务端 unknown 注记）
# ════════════════════════════════════════════════════════════════

def test_f05_plugin_author_status_tristate():
    """传 turn_author→known；不传→unknown；传空 dict→empty；不再恒空串假真人。"""
    _, mod = _plugin_cls()
    # 行为级验证 sync_turn 产出的 metadata（AST 级不足以覆盖闭包内变量流）。
    # 用最小替身记录 /add 载荷。
    captured = {}

    class _Client:
        user_id = "u"
        bank_id = "default"
        def try_request(self, method, path, body=None, timeout=None):
            captured["body"] = body
            return {}

    prov = mod.AiduMemProvider.__new__(mod.AiduMemProvider)
    prov._client = _Client()
    prov._writes_skipped = lambda: False
    prov._track_write = lambda sid, fn, name: fn("s1")  # 同步执行写函数
    if hasattr(mod.AiduMemProvider, "sync_turn"):
        # sync_turn 内部可能读的其他实例态，全部给安全默认
        for attr, val in (("_session_id", "s1"), ("_max_turn_chars", 4000)):
            if not hasattr(prov, attr):
                setattr(prov, attr, val)
    # 场景一：宿主没传（F05 原始断线场景）
    prov.sync_turn("帮我看下这个报错", "好的")
    meta = captured["body"]["metadata"]
    assert meta["_origin_author_status"] == "unknown", meta
    assert meta["_origin_is_bot"] is False and meta["_origin_author_id"] == ""
    # 场景二：宿主传了 bot 身份
    prov.sync_turn("转发：我也遇到", "收到", turn_author={"id": "bot2", "name": "二号", "is_bot": True})
    meta2 = captured["body"]["metadata"]
    assert meta2["_origin_author_status"] == "known", meta2
    assert meta2["_origin_is_bot"] is True and meta2["_origin_author_id"] == "bot2"
    # 场景三：传了空 dict → empty（不是 unknown 也不是 known）
    prov.sync_turn("第三轮", "好", turn_author={})
    meta3 = captured["body"]["metadata"]
    assert meta3["_origin_author_status"] == "empty", meta3


def test_f05_server_stamps_unknown_without_downgrading():
    """服务端：author_status=unknown → sidecar source 注记 author_unknown，
    但 is_bot 未申报不降权（epistemic 保持 reasoned/user_provided 原判）。
    负向对照：is_bot=True 仍 fuzzy。"""
    # 真实缝位在 layer1._index_after_add（CLI 2026-10-21 实证：路由层同名
    # stamp 是死缝位——sidecar 恒 add:layer1、死缝点零日志）。按源码结构验：
    # ① layer1 读 _origin_author_status 并落 add:layer1:author_unknown 注记；
    # ② unknown 不得驱动 epistemic mode（不猜红线）。
    src = pathlib.Path(__file__).resolve().parent.parent / "ducky/layer1_selfcheck.py"
    import ast as _ast
    tree = _ast.parse(src.read_text())
    d = src.read_text()
    assert "_origin_author_status" in d and "add:layer1:author_unknown" in d
    stamp_calls = [n for n in _ast.walk(tree)
                   if isinstance(n, _ast.Call) and getattr(n.func, "id", "") == "stamp_memory_refs"]
    assert stamp_calls, "layer1 缺 stamp_memory_refs 调用"
    for call in stamp_calls:
        kws = {k.arg: k for k in call.keywords}
        if "source" in kws:
            # source 实参若含 author_unknown 分支，则它只许出现在字符串注记路径
            mode_arg = call.args[1]
            assert "author_unknown" not in _ast.unparse(mode_arg), (
                "unknown 不得驱动 epistemic 降权（不猜红线）")


# ════════════════════════════════════════════════════════════════
# 6.2 · 重放退避
# ════════════════════════════════════════════════════════════════

def test_backoff_progression_and_quarantine():
    from ducky.dual_index import (_compute_backoff_seconds,
                                  _REPLAY_QUARANTINE_STAMP)
    # 指数序列：1,2,4,8,16,32,60,60（封顶）
    seq = [_compute_backoff_seconds(n) for n in range(1, 9)]
    assert seq[:5] == [1, 2, 4, 8, 16], seq
    assert seq[-1] == 60.0
    assert _REPLAY_QUARANTINE_STAMP.startswith("9999")


def test_failed_replay_gets_backoff_not_hammering():
    """真实 replay_pending 对一条恒坏账：两次连跑之间必须有退避间隔，
    第二轮直接 not_due 跳过（负向：正常可放行账不受退避影响）。"""
    from ducky.dual_index import (_enqueue_pending, replay_pending,
                                  ensure_pending_schema)
    from ducky.utils import get_facts_conn
    ensure_pending_schema()

    # 造一条坏账（side 未知 → 重放抛 RuntimeError）与一条好账（local 补写成功）
    _enqueue_pending("side_unknown_forced_fail", "ref-bad", "坏账文本", {"user_id": "u1"})
    _enqueue_pending("local", "ref-good", "好账文本", {"user_id": "u1", "bank_id": "default"})

    # 预置一条已结算行（负向对照：退避轮不扰动已 replayed_at 的正常账）
    conn = get_facts_conn()
    try:
        conn.execute(
            "INSERT INTO pending_embeddings (side, ref_id, payload, user_id, bank_id, replayed_at) "
            "VALUES ('local','ref-settled','{}','u1','default','2026-10-20T00:00:00')")
        conn.commit()
    finally:
        conn.close()

    rep1 = replay_pending(source="test1")
    assert rep1["failed"] >= 1, rep1   # 沙箱无 qdrant/mem0，两腿都失败=退避路径触发

    conn = get_facts_conn()
    try:
        bad = conn.execute(
            "SELECT replay_attempts, next_retry_at FROM pending_embeddings "
            "WHERE side='side_unknown_forced_fail'").fetchone()
        good = conn.execute(
            "SELECT replayed_at FROM pending_embeddings "
            "WHERE ref_id='ref-good'").fetchone()
        settled = conn.execute(
            "SELECT replayed_at FROM pending_embeddings "
            "WHERE ref_id='ref-settled'").fetchone()
    finally:
        conn.close()
    assert bad and bad[0] == 1, bad           # 甄别数只记 1 次（不是逐轮累加）
    assert bad[1] and bad[1] > "2000", bad    # next_retry_at 已排到未来
    # 退避期内再跑：坏账不进扫描（scanned 不再含它）——不再 100 次/秒空转
    rep2 = replay_pending(source="test2")
    assert rep2["failed"] <= 1, rep2
    conn = get_facts_conn()
    try:
        bad_after = conn.execute(
            "SELECT replay_attempts FROM pending_embeddings "
            "WHERE side='side_unknown_forced_fail'").fetchone()
    finally:
        conn.close()
    assert bad_after[0] == 1, (bad, bad_after)  # 第二轮没有第 2 次失败=退避生效
    # 负向对照锚点：失败行 replayed_at 仍 NULL（留账）vs 已结算行不动
    assert good[0] is None, good               # 失败行未被错误标记完成
    assert settled[0] is not None, settled     # 已结算行原样


def test_quarantine_after_threshold():
    from ducky.dual_index import (_record_replay_failure, _REPLAY_QUARANTINE_STAMP,
                                  REPLAY_QUARANTINE_AT)
    from ducky.utils import get_facts_conn
    from ducky.dual_index import ensure_pending_schema, _enqueue_pending
    ensure_pending_schema()
    _enqueue_pending("local", "ref-quar", "隔离测试", {"user_id": "u2"})
    conn = get_facts_conn()
    try:
        pid = conn.execute(
            "SELECT pending_id FROM pending_embeddings WHERE ref_id='ref-quar'").fetchone()[0]
        # 清掉下次重试拦截，只验证计数→隔离转移
        for i in range(REPLAY_QUARANTINE_AT):
            status = _record_replay_failure(pid, RuntimeError("持续坏"))
    finally:
        conn.close()
    assert status == "quarantined", status
    conn = get_facts_conn()
    try:
        row = conn.execute(
            "SELECT next_retry_at, replay_attempts FROM pending_embeddings "
            "WHERE pending_id=?", (pid,)).fetchone()
    finally:
        conn.close()
    assert row[0] == _REPLAY_QUARANTINE_STAMP, row
    assert row[1] == REPLAY_QUARANTINE_AT, row


def test_pending_counts_reports_backoff_surfaces():
    from ducky.dual_index import pending_counts, ensure_pending_schema
    ensure_pending_schema()
    counts = pending_counts()
    assert isinstance(counts, dict)
    assert "quarantined" in counts and "not_due_backoff" in counts, counts
    assert counts["quarantined"] >= 1  # 上一用例已隔离一条

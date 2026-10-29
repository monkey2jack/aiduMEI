"""f0.7 P6d：CC 空回复后自动补的催促句不是用户的话，写线必须剥掉。

生产实测（2026-10-29）：模型只出 thinking 没出正文时，CC 以 user 身份往
transcript 写「[Your previous response had no visible output. Please continue
and produce a user-visible response.]」，旧写线把它当最后一句用户话入库（10-18 起
14 个会话）。修复后：写线取的是真正的用户提问；催促句后面跟着真内容时只剥催促句。
"""
from __future__ import annotations

import json

from tests.test_f04pp_cc_adaptation import (
    _CC_HOOKS, _capture_server, _hook_env, _run_hook, _wait_for,
)

_NUDGE = ("[Your previous response had no visible output. "
          "Please continue and produce a user-visible response.]")


def _write(tp, rows):
    tp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                  encoding="utf-8")


def _u(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _a(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "..."}] + ([{"type": "text", "text": text}] if text else [])}}


def _ingest(tmp_path, rows):
    tp = tmp_path / "t.jsonl"
    _write(tp, rows)
    with _capture_server(lambda path: {"status": "accepted"}) as (port, calls):
        r = _run_hook(_CC_HOOKS / "aidumem-cc-ingest.sh",
                      {"session_id": "p6d", "transcript_path": str(tp), "hook_event_name": "Stop"},
                      _hook_env(port, tmp_path))
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        got = _wait_for(calls, lambda p: p.startswith("/add"), tries=30)
        adds = [json.loads(b.decode()) for p, b in calls if p.startswith("/add")]
    return got, adds


def test_nudge_alone_never_becomes_user_text(tmp_path):
    got, adds = _ingest(tmp_path, [_u("我的测试鹦鹉叫什么？"), _a(""), _u(_NUDGE), _a("")])
    assert got
    msgs = adds[0]["messages"]
    assert msgs[0] == {"role": "user", "content": "我的测试鹦鹉叫什么？"}
    assert all("no visible output" not in m["content"].lower() for m in msgs)


def test_nudge_prefix_stripped_real_text_kept(tmp_path):
    got, adds = _ingest(tmp_path, [_u(_NUDGE + " 部署成功了吗"), _a("成功了")])
    assert got
    assert adds[0]["messages"][0]["content"] == "部署成功了吗"
    assert adds[0]["messages"][1]["content"] == "成功了"


def test_unbracketed_lowercase_variant(tmp_path):
    got, adds = _ingest(tmp_path, [_u("真问题"), _a(""),
                                   _u(_NUDGE.strip("[]").lower()), _a("")])
    assert got
    assert adds[0]["messages"][0]["content"] == "真问题"


def test_only_nudge_session_writes_nothing(tmp_path):
    got, adds = _ingest(tmp_path, [_u(_NUDGE), _a("")])
    assert not got and not adds

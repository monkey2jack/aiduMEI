"""f0.8+ stream B: outside-audit R3 write-policy regressions (sesame appendix A, write-policy part)
plus our own cases both ways.  Every test asserts the SAFE behaviour."""
from __future__ import annotations

import hashlib
import json

import pytest


@pytest.fixture
def wp(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path))
    for k in ("AIDUMEI_WRITE_POLICY", "AIDUMEI_WRITE_POLICY_P8", "AIDUMEI_WRITE_POLICY_P1",
              "AIDUMEI_WRITE_POLICY_THINKING"):
        monkeypatch.delenv(k, raising=False)
    from ducky import write_policy
    return write_policy


def _p8(wp, text, uid="alice"):
    d = wp.gate_decision(text, "user", {}, user_id=uid, bank_id="default")
    return bool(d and d.get("principle") == "P8")


# ── R3-03: forget rule bypassed by fullwidth / Cyrillic look-alikes ─────────────────────
@pytest.mark.parametrize("text", [
    "my ex is wang xiaohua",                       # control
    "my \uff45\uff58 is \uff57\uff41\uff4e\uff47 \uff58\uff49\uff41\uff4f\uff48\uff55\uff41",  # fullwidth
    "my ex is w\u0430ng xi\u0430ohu\u0430",          # Cyrillic а (U+0430)
    "my e\u200bx is wang xiao\u200bhua",             # zero-width
    "MY EX IS WANG XIAOHUA",                        # case
    "my ex is wangxiaohua",                         # pinyin written together
    "my e\u0301x is wang xiaohua",                   # combining mark
])
def test_r3_03_forget_rule_survives_unicode_variants(wp, text):
    wp.add_rule("alice", "default", "ex wang xiaohua")
    assert _p8(wp, text)


def test_r3_03_rule_written_in_lookalikes_still_matches_plain_text(wp):
    wp.add_rule("alice", "default", "\uff34\u043em")        # fullwidth T + Cyrillic о
    assert _p8(wp, "Tom called again")


# ── R3-04: word boundaries, not substrings; blocked writes are restorable ─────────────────
@pytest.mark.parametrize("rule,text", [
    ("Tom", "remind me tomorrow about the customs form"),
    ("Tom", "the atom demo went fine"),
    ("Tom", "Tomás sent the slides"),
    ("ann", "the annual report is due"),
    ("ex wang xiaohua", "the next wang family dinner"),
])
def test_r3_04_forget_rule_no_false_positive(wp, rule, text):
    wp.add_rule("alice", "default", rule)
    assert not _p8(wp, text)


@pytest.mark.parametrize("rule,text", [
    ("Tom", "Tom called me again"),
    ("Tom", "和Tom吵架了"),
    ("Tom", "it was tom's idea"),
    ("Tom", "(Tom) said no"),
    ("前任 李雷", "我前任是李雷"),
    ("青石巷 九号", "我的旧住址是青石巷九号二单元"),
])
def test_r3_04_forget_rule_still_hits(wp, rule, text):
    wp.add_rule("alice", "default", rule)
    assert _p8(wp, text)


def test_r3_04_p8_goes_to_a_restorable_tombstone(wp):
    rid = wp.add_rule("alice", "default", "Tom")
    d = wp.gate_decision("Tom called me again", "user", {}, user_id="alice", bank_id="default")
    assert d == {"action": "tombstone", "principle": "P8", "reason": "forget_rule", "rule": rid}
    kept, rec = wp.enforce_messages("Tom called me again", {}, user_id="alice", bank_id="default")
    assert kept == "" and rec[0]["tombstone_id"] and rec[0]["rule"] == rid
    from ducky.tombstone import list_tombstones
    rows = list_tombstones(user_id="alice", bank_id="default")
    hit = [r for r in rows if r.get("tombstone_id") == rec[0]["tombstone_id"]]
    assert hit and str(hit[0].get("reason", "")).startswith("write_policy:P8:forget_rule")


# ── R3-06: an empty env var must not downgrade enforce to shadow ────────────────────────
@pytest.mark.parametrize("name", ["P1", "P8", "THINKING"])
@pytest.mark.parametrize("val", ["", "  ", "enforec", "maybe"])
def test_r3_06_empty_or_garbled_env_keeps_enforce(wp, monkeypatch, name, val):
    monkeypatch.setenv(f"AIDUMEI_WRITE_POLICY_{name}", val)
    assert wp.principle_mode(name) == "enforce"


@pytest.mark.parametrize("val", ["shadow", "SHADOW", "off", "0", "false", "no"])
def test_r3_06_explicit_downgrade_still_works(wp, monkeypatch, val):
    monkeypatch.setenv("AIDUMEI_WRITE_POLICY_P8", val)
    assert wp.principle_mode("P8") == "shadow"


# ── R3-05: the summary flag no longer lifts the raw-length cap ───────────────────────────
def test_r3_05_summary_flag_does_not_lift_length_cap(wp):
    big = "This is the full raw contract text, clause one, party A and party B agree. " * 330
    assert wp.hard_block_reason(big) == "oversized_raw_copy"
    assert wp.hard_block_reason(big, summary=True) == "oversized_raw_copy"


def test_r3_05_real_summary_size_still_ok(wp):
    assert wp.hard_block_reason("本次会话讨论了搬家安排。" * 40, summary=True) is None


def test_r3_05_seal_binds_text_scope_and_fingerprint(wp):
    from ducky import distill_seal as ds
    md = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill",
          "distill_mode": "llm", "distill_source_fingerprint": "fp1"}
    md[ds.SEAL_FIELD] = ds.seal("用户周末要搬家。", "alice", "default", md)
    assert ds.verify("用户周末要搬家。", "alice", "default", md)
    assert ds.verify([{"role": "user", "content": "用户周末要搬家。"}], "alice", "default", md)
    assert not ds.verify("用户周末要搬家，合同全文如下……", "alice", "default", md)     # other text
    assert not ds.verify("用户周末要搬家。", "bob", "default", md)                     # other user
    assert not ds.verify("用户周末要搬家。", "alice", "work", md)                      # other bank
    assert not ds.verify("用户周末要搬家。", "alice", "default", {**md, "distill_source_fingerprint": "x"})
    assert not ds.verify("用户周末要搬家。", "alice", "default", {**md, "kind": "note"})
    assert not ds.verify(["a", "b"], "alice", "default", md)                          # not one summary


def test_r3_05_settle_strips_unsigned_summary_identity(wp):
    from ducky import distill_seal as ds
    from ducky.origin_context import is_session_summary
    md = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill",
          "distill_source_refs": ["verbatim:1"], "distill_mode": "llm", "topic": "x"}
    assert ds.settle(md, "整篇合同原文……", "alice", "default") is False
    assert not is_session_summary(md)
    assert md == {"topic": "x", "source_type": "inferred", "basis": "caller_declared:unsigned_summary"}
    good = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill", "distill_mode": "llm"}
    good[ds.SEAL_FIELD] = ds.seal("一句精华。", "alice", "default", good)
    assert ds.settle(good, "一句精华。", "alice", "default") is True
    assert is_session_summary(good) and ds.SEAL_FIELD not in good          # the seal is never stored
    plain = {"topic": "y"}
    assert ds.settle(plain, "hello", "alice", "default") is False and plain == {"topic": "y"}


def test_r3_05_distill_session_output_carries_a_valid_seal(wp, monkeypatch):
    from ducky import distill_seal as ds
    from ducky import session_distill as sd
    rows = [{"ref": f"verbatim:{i}", "text": f"第{i}句：我们决定周六搬家", "turn": i} for i in range(1, 5)]
    monkeypatch.setattr(sd, "collect_session_memories", lambda *a, **k: rows)
    monkeypatch.setattr(sd, "_latest_generation", lambda *a, **k: {})
    import ducky.llm_client as llm
    monkeypatch.setattr(llm, "call_llm", lambda *a, **k: "用户决定周六搬家。")
    import ducky.core_memory as cm
    monkeypatch.setattr(cm, "autosink_from_distill", lambda *a, **k: {"status": "skipped"})
    out = sd.distill_session("s1", user_id="alice", bank_id="default")
    assert out["status"] == "ok"
    assert ds.verify(out["summary"], "alice", "default", out["metadata"])
    assert not ds.verify(out["summary"] + " 外加一整篇原文", "alice", "default", out["metadata"])


# ── R3-08: who asserted "the user said" is decided server-side ──────────────────────────
def test_r3_08_client_cannot_preset_role_asserted_by(wp):
    md = {"role_asserted_by": "ui_session"}
    wp.neutralize_client_provenance(md)
    assert "role_asserted_by" not in md


def test_r3_08_caller_assertion_by_credential(wp, monkeypatch):
    from ducky import agent_keys
    from ducky.security import auth
    try:
        auth.clear_request_auth_kind()
        agent_keys.clear_current()
        assert wp.caller_assertion() == "local"
        auth.set_request_auth_kind("session")
        assert wp.caller_assertion() == "ui_session"
        auth.set_request_auth_kind("bearer")
        agent_keys.set_current(None, "fp-main")
        assert wp.caller_assertion() == "token"
        agent_keys.set_current({"agent": "helper", "callers": ["helper"]}, "fp-helper")
        assert wp.caller_assertion() == "agent_key:helper"
    finally:
        auth.clear_request_auth_kind()
        agent_keys.clear_current()


def test_r3_08_agent_key_user_turns_are_inferred(wp):
    assert wp.source_of({"role_asserted_by": "agent_key:helper"}) == ("inferred", "asserted_by:agent_key:helper")
    assert wp.source_of({"role_asserted_by": "token"}) == ("direct", "verbatim_turn")
    assert wp.source_of({"role_asserted_by": "ui_session"}) == ("direct", "verbatim_turn")
    assert wp.classify("我喜欢绿茶", "user", {"role_asserted_by": "agent_key:helper"})["source_type"] == "inferred"


# ── R3-09: numeric values under credential keys in metadata ─────────────────────────────
@pytest.mark.parametrize("md,secret", [({"pin": 4821}, "4821"), ({"password": 12345678}, "12345678"),
                                       ({"db_password": 98765432}, "98765432"),
                                       ({"nested": {"cvv": 731}}, "731"),
                                       ({"list": [{"secret_key": 123456789012}]}, "123456789012")])
def test_r3_09_numeric_metadata_secret_redacted(wp, md, secret):
    assert secret not in json.dumps(wp.redact_metadata(md))


def test_r3_09_plain_numbers_untouched(wp):
    md = {"count": 4821, "page": 12345678, "max_tokens": 4096, "pin_count": 5, "token_limit": 32000,
          "flag": True, "ratio": 0.5}
    assert wp.redact_metadata(md) == md


# ── R3-10 / plan ⑫: seed phrases ─────────────────────────────────────────────────────────
WORDS12 = "abandon ability able about above absent absorb abstract absurd abuse access accident"
VALID12 = "letter advice cage absurd amount doctor acoustic avoid letter advice cage above"   # BIP39 test vector
VALID24 = ("legal winner thank year wave sausage worth useful legal winner thank year wave sausage worth "
           "useful legal winner thank year wave sausage worth title")


def test_r3_10_mnemonic_with_longer_lead_in(wp):
    _, kinds = wp.redact_forbidden("这是我钱包的助记词，千万别告诉任何人，内容如下：" + WORDS12)
    assert kinds


@pytest.mark.parametrize("text,secret", [
    (VALID12, "advice cage absurd"),
    ("my backup words " + VALID24 + " keep safe", "sausage worth useful"),
    ("1. letter\n2. advice\n3. cage\n4. absurd\n5. amount\n6. doctor\n7. acoustic\n8. avoid\n"
     "9. letter\n10. advice\n11. cage\n12. above", "acoustic"),
    (VALID12.title(), "Advice Cage Absurd"),
    (VALID12.upper(), "ADVICE CAGE ABSURD"),
])
def test_bip39_phrase_without_keyword_is_redacted(wp, text, secret):
    out, kinds = wp.redact_forbidden(text)
    assert "seed_phrase" in kinds and secret not in out, out


@pytest.mark.parametrize("text", [
    WORDS12,                                                          # BIP39 words, bad checksum
    "apple, banana, bread, butter, cheese, chicken, coffee, cream, egg, honey, lemon, milk",  # a list
    "I need to buy apple banana bread butter and cheese for the picnic tomorrow",
    "the mnemonic is easy to remember when you have a good teacher and some time to practise daily",
    "letter advice cage absurd amount doctor",                        # too short
])
def test_bip39_ordinary_text_untouched(wp, text):
    assert wp.redact_forbidden(text) == (text, [])


def test_bip39_wordlist_is_the_upstream_file(wp):
    from ducky.bip39_english import UPSTREAM_SHA256, WORDS, WORDSET
    assert len(WORDS) == 2048 == len(WORDSET)
    assert hashlib.sha256(("\n".join(WORDS) + "\n").encode()).hexdigest() == UPSTREAM_SHA256
    assert WORDS[0] == "abandon" and WORDS[-1] == "zoo"


# ── plan ⑫: ordinary English after a password-like key is not a password ─────────────────
@pytest.mark.parametrize("text", [
    "the password is printed on the back of the router",
    "my wifi password is pretty long",
    "password is something I always forget",
    "the passphrase is quite long actually",
    "password was taken from the sticky note",
    "the password is written down somewhere",
    "password is usually my birthday",
    "password: check the email I sent",
    "the password is printed",
])
def test_password_word_followed_by_prose_not_redacted(wp, text):
    assert wp.redact_forbidden(text) == (text, [])


@pytest.mark.parametrize("text,secret", [
    ("password is hunter", "hunter"),
    ("password is sunshine", "sunshine"),
    ("password is dragonfly and that is it", "dragonfly"),
    ("password is hunter but dont tell", "hunter"),
    ("my gmail password is tulips.", "tulips"),
    ("password: sunshine", "sunshine"),
    ('password: "printed"', "printed"),                      # quoted: taken literally
    ("password: Hunter2024!x please", "Hunter2024!x"),
    ("我的wifi密码是 sunshine", "sunshine"),
    ("the password is hunter2 for now", "hunter2"),
])
def test_real_passwords_still_redacted(wp, text, secret):
    out, kinds = wp.redact_forbidden(text)
    assert kinds and secret not in out, out


# ── R3-11: Unicode hyphens, split and encoded keys (best effort) ────────────────────────
KEY = "sk-ant-api03-Zq8vT3mK9pL2xW7nR4bY6cJ1abcd"


@pytest.mark.parametrize("dash", ["\u2010", "\u2011", "\u2012", "\u2013", "\u2212", "\ufe63", "\uff0d"])
def test_r3_11_unicode_hyphen_variants(wp, dash):
    out, kinds = wp.redact_forbidden(f"key {KEY.replace('-', dash)} ok")
    assert "api_key" in kinds and "Zq8vT3mK9pL2xW7nR4bY6cJ1" not in out


def test_r3_11_em_dash_separator_still_works(wp):
    out, kinds = wp.redact_forbidden("密码——Hunter2024!x")
    assert kinds and "Hunter2024" not in out


def _enc_cases():
    import base64
    from urllib.parse import quote
    return [
        f"cfg {base64.b64encode(KEY.encode()).decode()} end",
        f"cfg {base64.urlsafe_b64encode(KEY.encode()).decode().rstrip('=')} end",
        f"hex {KEY.encode().hex()} end",
        f"rev {KEY[::-1]} end",
        'key = "sk-ant-" + "api03-Zq8vT3mK9pL2xW7nR4bY6cJ1abcd"',
        "token = 'ghp_' + 'Zq8vT3mK9pL2xW7nR4bY6cJ1abcdefghij'",
        "url " + quote("ghp_Zq8vT3mK9pL2xW7nR4bY6cJ1abcdefghij", safe="").replace("_", "%5F") + " x",  # release-scan:allow synthetic security fixture, no production credential/address
    ]


@pytest.mark.parametrize("idx", range(7))
def test_r3_11_encoded_or_concatenated_keys(wp, idx):
    text = _enc_cases()[idx]
    out, kinds = wp.redact_forbidden(text)
    assert kinds, out
    assert "Zq8vT3mK9pL2xW7nR4bY6cJ1" not in out and "1Jc6Yb4Rn7Wx2Lp9Km3Tv8qZ" not in out


@pytest.mark.parametrize("text", [
    "commit 9f8e7d6c5b4a39281706f8e7d6c5b4a392817061 merged",
    "see https://example.com/a%20b%20c%20d",
    "aGVsbG8gd29ybGQgdGhpcyBpcyBqdXN0IGEgbm9ybWFsIHNlbnRlbmNl",     # base64 of an ordinary sentence
    "name = 'a' + 'b' + 'c'",
    "path /usr/local/lib/python3.12/site-packages/x",
    "sha256 2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda",
])
def test_r3_11_ordinary_long_strings_untouched(wp, text):
    assert wp.redact_forbidden(text) == (text, [])

"""f0.8 ② write-policy enforcement: pure rules (no app).

Reverse cases come from the shadow week, as generic synthetic equivalents only
(no real chat text, names or nicknames):
  68 machine-noise items → gated (enforced) ;  short confirmations → daily layer ;
  model thinking text → rejected ;  (8 replace/conflict false pairs: test_f08_supersede_topic.py)
  emotional / commitment sentences → kept.
"""
import pytest

from ducky import write_policy as wp

# ── 68 machine-noise items (44 [SILENT] + 9 image placeholders + 15 continuation notices) ──
_SILENT = ["[SILENT]", " [SILENT]", "[SILENT] ", "[SILENT]\n", "\n[SILENT]", "[silent]", "[Silent]", "  [SILENT]  "]
SILENT = [_SILENT[i % len(_SILENT)] for i in range(44)]
_DIMS = [(2076, 1476), (1080, 2348), (2392, 184), (1920, 1080), (750, 1334), (3024, 4032), (1284, 2778),
         (800, 600), (2560, 1600)]
IMAGES = [f"[Image: original {w}x{h}, displayed at {min(w, 2000)}x{int(h * min(w, 2000) / w)}.]" for w, h in _DIMS]
_CONT_TAILS = ["", " The summary below covers the earlier portion of the conversation.",
               " Summary:\n1. Primary request and intent: continue the previous task.",
               " that ran out of context.", " The conversation is summarized below:\nAnalysis: the work so far."]
CONTINUATIONS = [f"This session is being continued from a previous conversation{_CONT_TAILS[i % 5]}"
                 + (f" (part {i})" if i >= 5 else "") for i in range(15)]
MACHINE_NOISE = SILENT + IMAGES + CONTINUATIONS

SHORT_CONFIRMATIONS = ["好，那走吧", "改", "好", "嗯", "行", "可以", "好的", "继续", "收到", "ok"]

THINKING = [
    "We need to output one or two sentences summarizing the session in Chinese.",
    "We need to produce a concise summary. The user wants the key decision only.",
    "Let me think about what matters most here. The user wants a short summary.",
    "The user wants a summary. We should output it in Chinese, under 80 characters, one or two sentences.",
    "Okay, so the main point is the deploy. Let me write it.",
    "<think>The user wants a summary of the session.</think>",
    "analysis<|message|>We need to summarize the session.<|end|>",
    "First, I need to identify the key event of this session.",
]

KEPT = [
    "我答应周末陪家人去湖边，说好了不再加班",
    "以后每次部署前都要先备份，这是我们的规矩",
    "我会一直记得这一天，谢谢你陪我把它做完",
    "我对花生过敏，点外卖时要避开花生",
    "我们决定新版本先上影子模式，观察一周再拦截",
    "说好了明年春天一起去看海",
    "用户决定新版本先上影子模式，观察一周后再开拦截。",
    "I promise I will call you every Sunday.",
]


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for k in ("AIDUMEI_WRITE_POLICY", "AIDUMEI_WRITE_POLICY_P1", "AIDUMEI_WRITE_POLICY_P8",
              "AIDUMEI_WRITE_POLICY_THINKING"):
        monkeypatch.delenv(k, raising=False)


SUMMARY_MD = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill",
              "distill_mode": "llm", "distill_source_count": 4}


def test_shadow_week_noise_count():
    assert len(MACHINE_NOISE) == 68


@pytest.mark.parametrize("text", MACHINE_NOISE)
def test_machine_noise_is_gated_to_tombstone(text):
    d = wp.gate_decision(text, "user", {}, user_id="u", bank_id="default")
    assert d and d["action"] == "tombstone" and d["principle"] == "P1"
    assert d["reason"] in wp.ENFORCED_NOISE


@pytest.mark.parametrize("text", SHORT_CONFIRMATIONS)
def test_short_confirmations_go_to_daily_not_gated(text):
    assert wp.gate_decision(text, "user", {}, user_id="u", bank_id="default") is None
    v = wp.classify(text)
    assert v["verdict"] == "daily" and v["layer"] == "daily"


@pytest.mark.parametrize("text", KEPT)
def test_emotional_and_commitment_sentences_are_kept(text):
    assert wp.gate_decision(text, "user", {}, user_id="u", bank_id="default") is None
    assert wp.gate_decision(text, "user", SUMMARY_MD, user_id="u", bank_id="default") is None
    assert wp.classify(text)["verdict"] != "reject"
    assert wp.looks_like_reasoning(text) is None


@pytest.mark.parametrize("text", THINKING)
def test_thinking_text_is_detected(text):
    assert wp.looks_like_reasoning(text)


@pytest.mark.parametrize("text", THINKING)
def test_thinking_summary_is_gated_only_on_the_summary_path(text):
    d = wp.gate_decision(text, "assistant", SUMMARY_MD, user_id="u", bank_id="default")
    assert d and d["principle"] == "THINKING" and d["action"] == "tombstone"
    # a user's own words are never judged as "thinking"
    d2 = wp.gate_decision(text, "user", {}, user_id="u", bank_id="default")
    assert d2 is None or d2["principle"] != "THINKING"
    # a fallback summary quotes the user's turns: not checked either
    assert wp.gate_decision(text, "assistant", {**SUMMARY_MD, "distill_mode": "fallback"},
                            user_id="u", bank_id="default") is None


def test_strip_reasoning_keeps_the_answer():
    assert wp.strip_reasoning("<think>We need to x</think>用户决定先上影子模式。") == "用户决定先上影子模式。"
    assert wp.strip_reasoning("analysis We need to output. assistantfinal 用户决定先上影子模式。") == "用户决定先上影子模式。"
    assert wp.strip_reasoning("<|channel|>analysis<|message|>We need…<|end|><|start|>assistant<|channel|>final"
                              "<|message|>这一程定下了发布节奏。") == "这一程定下了发布节奏。"
    assert wp.strip_reasoning("<think>only thinking, no answer") == ""


@pytest.mark.parametrize("env,principle,text,md", [
    ("AIDUMEI_WRITE_POLICY_P1", "P1", "[SILENT]", {}),
    ("AIDUMEI_WRITE_POLICY_THINKING", "THINKING", THINKING[0], SUMMARY_MD),
])
def test_each_enforced_principle_can_go_back_to_shadow(monkeypatch, env, principle, text, md):
    assert wp.principle_mode(principle) == "enforce"
    monkeypatch.setenv(env, "shadow")
    assert wp.principle_mode(principle) == "shadow"
    assert wp.gate_decision(text, "assistant", md, user_id="u", bank_id="default") is None
    monkeypatch.delenv(env)
    monkeypatch.setenv("AIDUMEI_WRITE_POLICY", "off")
    assert wp.principle_mode(principle) == "off"
    assert wp.gate_decision(text, "assistant", md, user_id="u", bank_id="default") is None


def test_p8_forget_rule_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path))
    rid = wp.add_rule("u", "default", "旧门牌 三单元")
    d = wp.gate_decision("我以前住的旧门牌是七号三单元", "user", {}, user_id="u", bank_id="default")
    assert d == {"action": "drop", "principle": "P8", "reason": "forget_rule", "rule": rid}
    # another tenant's rule never applies
    assert wp.gate_decision("我以前住的旧门牌是七号三单元", "user", {}, user_id="v", bank_id="default") is None
    # only all of the rule's words together trigger it
    assert wp.gate_decision("三单元的电梯坏了", "user", {}, user_id="u", bank_id="default") is None
    kept, receipts = wp.enforce_messages(
        [{"role": "user", "content": "我以前住的旧门牌是七号三单元"}, {"role": "user", "content": "明天开会"}],
        {}, user_id="u", bank_id="default")
    assert kept == [{"role": "user", "content": "明天开会"}]
    assert receipts == [{"action": "drop", "principle": "P8", "reason": "forget_rule", "rule": rid}]
    rows = wp.list_verdicts("u", "default")
    assert rows[0]["mode"] == "enforce" and rows[0]["blacklist_rule"] == rid
    assert rows[0]["content_sha"] is None and rows[0]["length"] == 0      # forgotten: no hash, no text
    monkeypatch.setenv("AIDUMEI_WRITE_POLICY_P8", "shadow")
    assert wp.gate_decision("我以前住的旧门牌是七号三单元", "user", {}, user_id="u", bank_id="default") is None


def test_enforce_messages_shapes_and_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path))
    calls = []

    def fake_apply(text, role, md, decision, user_id, bank_id):
        calls.append(text)
        return {"action": decision["action"], "principle": decision["principle"], "reason": decision["reason"]}
    monkeypatch.setattr(wp, "_apply_gate", fake_apply)
    assert wp.enforce_messages("[SILENT]", {}, user_id="u", bank_id="default")[0] == ""
    assert wp.enforce_messages({"role": "user", "content": "[SILENT]"}, {}, user_id="u", bank_id="default")[0] is None
    kept, rec = wp.enforce_messages([{"role": "system", "content": "x"}, {"role": "user", "content": "[SILENT]"}],
                                    {}, user_id="u", bank_id="default")
    assert kept == [] and len(rec) == 1 and wp.nothing_left(kept)
    kept, rec = wp.enforce_messages("我答应周末陪家人", {}, user_id="u", bank_id="default")
    assert kept == "我答应周末陪家人" and rec == []

    def boom(*_a, **_k):
        raise OSError("disk")
    monkeypatch.setattr(wp, "_apply_gate", boom)
    kept, rec = wp.enforce_messages([{"role": "user", "content": "[SILENT]"}], {}, user_id="u", bank_id="default")
    assert kept == [{"role": "user", "content": "[SILENT]"}] and rec == []      # fail-open: stored as before

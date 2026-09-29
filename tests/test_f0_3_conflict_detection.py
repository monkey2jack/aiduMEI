"""f0.3 A2 -- conflict detection rewrite (ducky/salience/conflict.py).

Production facts pinned here:
  * the old lexical detector flagged ~88,424 "conflict" pairs per run on ~2,600
    memories: single-character antonym pairs, and negated forms counted as the
    positive form (the negated "shi" form literally contains "shi");
  * every flagged pair halved BOTH memories' salience, so a memory caught in k
    pairs was multiplied by 0.5**k -- and low salience feeds eviction.

Every behaviour below is paired with a negative control: a verbatim copy of the
OLD pair list + matching loop is run on the same inputs and must misbehave,
which proves the inputs really exercise the old defect (a test that the old
code would also pass proves nothing).
"""
from __future__ import annotations

import time

import pytest

from ducky import utils
from ducky.salience import conflict as conflict_mod
from ducky.salience import db as salience_db

# ── 任务书点名的四个用例 ──
NOT_CONFLICT_CASES = [
    ("老王说不要熬夜", "老王说不要喝酒"),    # 两条都是否定式：「不要」里的「要」不是肯定
    ("这不是bug", "这不是问题"),            # 两条都是否定式：「不是」里的「是」不是肯定
    ("大刘喜欢喝咖啡", "小李负责部署"),      # 单字「大/小」已删；也根本不是一件事
]
TRUE_CONFLICT = ("老王喜欢喝咖啡", "老王不喜欢喝咖啡")

# ── 更多行为钉子 ──
MORE_NOT_CONFLICT = [
    ("大刘喜欢喝咖啡", "老王不喜欢喝咖啡"),              # Jaccard 恰好 0.5，但主语不同
    ("老王喜欢喝咖啡", "小李不喜欢喝咖啡"),              # 主语不同
    ("老王不喜欢喝咖啡", "老王不喜欢喝咖啡但喜欢喝茶"),  # 排他：后者自己也否定了「喜欢」
    ("备份没成功", "备份失败"),                          # 说的是同一个意思
    ("老王说过很多次了不要熬夜", "老王说过很多次了要早睡"),  # 共享前文，谓词不同
    ("不过要早睡", "要早睡"),                            # 「不过」不是否定
    ("未来要上线", "上线"),                              # 「未来」不是否定
    ("2026-09-01 老王喜欢喝咖啡", "2026-09-01 小李不喜欢喝咖啡"),  # 同日期前缀不算同一主语
    # 以下四条是本判据自己的回归：写第一版时在合成语料上实测撞出来的假阳性
    ("小李要先在本地测试，老王喜欢大屏幕", "aiduMEI要先在本地测试，老王不喜欢加班"),  # 按子句比，不按整条
    ("consolidator不喜欢加班", "consolidator喜欢大屏幕"),    # 英文整词算一个词元
    ("北城生产机不喜欢加班", "北城生产机喜欢大屏幕"),        # 宾语不同
    ("小李测试失败了", "小李部署成功"),
]
MORE_CONFLICT = [
    ("睡前要刷牙", "睡前不要刷牙"),
    ("我有车", "我没有车"),
    ("昨晚的备份任务成功", "昨晚的备份任务失败"),
    ("老王喜欢喝咖啡", "老王不太喜欢喝咖啡"),
    ("老王喜欢喝咖啡", "老王现在不喜欢喝咖啡"),
    ("备份没成功", "备份成功"),
    ("两个库的路径相同", "两个库的路径不同"),
    ("这是bug", "这不是bug"),
    ("部署成功了", "部署失败了"),
    ("老王不喜欢加班，喜欢大屏幕", "老王喜欢加班"),              # 子句级排他
    ("今天部署了aiduMEI。老王喜欢喝咖啡", "老王不喜欢喝咖啡"),  # 矛盾在其中一个子句里
    ("老王说，不要熬夜", "老王说，要熬夜"),
]

# 多子句合成语料：主语 × 说法两两拼成一条（构造上不含真矛盾）
MULTI_SUBJECTS = ["老王", "小李", "阿美", "大刘", "服务器", "北城生产机", "consolidator", "aiduMEI"]
MULTI_NOTES = [
    "说不要熬夜", "觉得这不是问题", "负责部署", "喜欢大屏幕", "不喜欢加班", "有很多想法",
    "没有时间开会", "能写代码", "记得备份数据", "修复了bug，日志正常", "部署成功", "测试失败了",
    "不能直接改服务器", "要先在本地测试", "用 上游模型 做抽取", "把阈值调到 0.46",
]
INJECTED_REAL = [
    ("阿美修复了bug，日志正常，老王喜欢喝咖啡", "老王不喜欢喝咖啡"),
    ("2026-09-02 consolidator部署成功", "2026-09-03 consolidator部署失败"),
    ("小李说不要熬夜，上游模型 限并发 5", "小李说要熬夜"),
]

# 一条记忆同时卷进多对矛盾（验「每轮最多 ×0.5 一次」）
HUB_TEXT = "老王喜欢喝咖啡"
SPOKE_TEXTS = ["老王不喜欢喝咖啡", "老王不太喜欢喝咖啡", "老王现在不喜欢喝咖啡"]

# 生产形态的小语料：主语 × 常见说法（大量「不要/不是/大/小/多/少/有/没有」）
CORPUS_SUBJECTS = ["老王", "小李", "阿美", "大刘"]
CORPUS_NOTES = [
    "说不要熬夜", "说不要喝酒", "觉得这不是问题", "确认这不是bug", "负责部署",
    "喜欢大屏幕", "用小号测试", "有很多想法", "少喝咖啡", "没有时间开会",
    "能写代码", "记得备份数据",
]
REAL_PAIR = ("阿美喜欢用蓝色主题", "阿美不喜欢用蓝色主题")

# f0.3 之前的反义词表，逐字照抄 —— 只给负向对照当「旧判据」用
OLD_ANTONYM_PAIRS = [
    ("开", "关"), ("启用", "禁用"), ("允许", "禁止"), ("要", "不要"),
    ("是", "不是"), ("有", "没有"), ("能", "不能"), ("记得", "忘记"),
    ("成功", "失败"), ("对", "错"), ("真", "假"), ("新", "旧"),
    ("快", "慢"), ("大", "小"), ("多", "少"),
]


def _old_detector_flags(text_a: str, text_b: str):
    """Verbatim logic of the pre-f0.3 detect_conflicts inner loop."""
    for pos, neg in OLD_ANTONYM_PAIRS:
        a_pos, a_neg = pos in text_a, neg in text_a
        b_pos, b_neg = pos in text_b, neg in text_b
        if (a_pos and b_neg) or (a_neg and b_pos):
            return f"{pos}<->{neg}"
    return None


@pytest.fixture(autouse=True)
def _fresh_salience(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "SALIENCE_DB", str(tmp_path / "salience.db"))
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(tmp_path / "no-such.env"))
    monkeypatch.delenv("AIDUMEI_CONFLICT_PENALTY_MODE", raising=False)
    monkeypatch.delenv("AIDUMEI_CONFLICT_MAX_PAIRS_PER_LANE", raising=False)
    salience_db._ensure_db()
    yield


def _insert(rows, *, salience=0.8, lane="general", user_id="default", bank_id="default"):
    conn = utils.get_salience_conn()
    now = time.time()
    for mid, content in rows:
        conn.execute(
            "INSERT OR REPLACE INTO salience (memory_id, salience, last_access, access_count, "
            "created_at, lane, content_preview, user_id, bank_id) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (mid, salience, now, now, lane, content, user_id, bank_id),
        )
    conn.commit()


def _salience_of(mid: str) -> float:
    conn = utils.get_salience_conn()
    return conn.execute("SELECT salience FROM salience WHERE memory_id=?", (mid,)).fetchone()[0]


def _pairs(conflicts):
    return {frozenset((c["memory_a"], c["memory_b"])) for c in conflicts}


# ---------------------------------------------------------------- the four required cases
@pytest.mark.parametrize("text_a, text_b", NOT_CONFLICT_CASES)
def test_required_false_positives_are_not_conflicts(text_a, text_b):
    assert conflict_mod.judge_pair(text_a, text_b) is None
    assert conflict_mod.judge_pair(text_b, text_a) is None
    # negative control: the old detector DID flag exactly these inputs
    assert _old_detector_flags(text_a, text_b) is not None


def test_required_true_conflict_is_detected():
    a, b = TRUE_CONFLICT
    hit = conflict_mod.judge_pair(a, b)
    assert hit is not None and hit["kind"] == "negation"
    assert hit["similarity"] >= conflict_mod._SAME_TOPIC_MIN_JACCARD
    assert conflict_mod.judge_pair(b, a) is not None, "verdict must be symmetric"
    # contrast: the old lexical list never even saw this contradiction, so the
    # rewrite is not merely "stricter" -- it is more correct in both directions
    assert _old_detector_flags(a, b) is None


@pytest.mark.parametrize("text_a, text_b", MORE_NOT_CONFLICT)
def test_more_non_conflicts(text_a, text_b):
    assert conflict_mod.judge_pair(text_a, text_b) is None


@pytest.mark.parametrize("text_a, text_b", MORE_CONFLICT)
def test_more_real_conflicts(text_a, text_b):
    assert conflict_mod.judge_pair(text_a, text_b) is not None


def test_no_single_character_antonym_pair_survives():
    assert all(len(pos) > 1 and len(neg) > 1 for pos, neg in conflict_mod._ANTONYM_PAIRS)
    # negative control: the old list had them
    assert any(len(pos) == 1 for pos, _neg in OLD_ANTONYM_PAIRS)


# ---------------------------------------------------------------- scanning real salience rows
def test_scan_over_salience_rows_flags_only_the_real_contradiction():
    rows = [(f"fp-{i}-{k}", text) for i, pair in enumerate(NOT_CONFLICT_CASES)
            for k, text in enumerate(pair)]
    rows += [("tc-a", TRUE_CONFLICT[0]), ("tc-b", TRUE_CONFLICT[1])]
    _insert(rows)
    scan = conflict_mod.scan_conflicts()
    assert _pairs(scan["conflicts"]) == {frozenset(("tc-a", "tc-b"))}
    assert scan["pairs_found"] == 1
    assert scan["pairs_compared"] == scan["pairs_total"] == len(rows) * (len(rows) - 1) // 2
    assert scan["truncated"] is False
    # negative control: over the same rows the old detector flags exactly the three
    # required false pairs -- and misses the one real contradiction
    old = {frozenset((rows[i][0], rows[j][0])) for i in range(len(rows))
           for j in range(i + 1, len(rows)) if _old_detector_flags(rows[i][1], rows[j][1])}
    assert old == {frozenset((f"fp-{i}-0", f"fp-{i}-1")) for i in range(len(NOT_CONFLICT_CASES))}


def test_production_shaped_corpus_old_explodes_new_stays_quiet():
    rows = [(f"m{i:03d}", f"{subject}{note}") for i, (subject, note) in
            enumerate((s, n) for s in CORPUS_SUBJECTS for n in CORPUS_NOTES)]
    rows += [("real-a", REAL_PAIR[0]), ("real-b", REAL_PAIR[1])]
    _insert(rows)
    new = conflict_mod.scan_conflicts()
    old = sum(1 for i in range(len(rows)) for j in range(i + 1, len(rows))
              if _old_detector_flags(rows[i][1], rows[j][1]))
    assert frozenset(("real-a", "real-b")) in _pairs(new["conflicts"])
    assert new["pairs_found"] <= 3, [c["word_pair"] for c in new["conflicts"]]
    assert old >= 20 * max(new["pairs_found"], 1), (old, new["pairs_found"])


def test_multi_clause_corpus_has_no_false_hits_but_keeps_real_ones():
    import random

    rng = random.Random(7)
    texts = [f"2026-09-{rng.randint(1, 28):02d} {rng.choice(MULTI_SUBJECTS)}{rng.choice(MULTI_NOTES)}，"
             f"{rng.choice(MULTI_SUBJECTS)}{rng.choice(MULTI_NOTES)}" for _ in range(300)]
    feats = [conflict_mod._features(t) for t in texts]
    new_hits = [(texts[i], texts[j]) for i in range(len(feats)) for j in range(i + 1, len(feats))
                if conflict_mod._judge(feats[i], feats[j])]
    assert new_hits == [], new_hits[:3]
    for a, b in INJECTED_REAL:
        assert conflict_mod.judge_pair(a, b) is not None, (a, b)
    # negative control: the old detector drowns this corpus in "conflicts"
    old_hits = sum(1 for i in range(len(texts)) for j in range(i + 1, len(texts))
                   if _old_detector_flags(texts[i], texts[j]))
    assert old_hits > 1000, old_hits


# ---------------------------------------------------------------- penalty modes
def test_default_mode_is_warn_and_changes_no_salience():
    _insert([("tc-a", TRUE_CONFLICT[0]), ("tc-b", TRUE_CONFLICT[1])])
    conflicts = conflict_mod.detect_conflicts()
    assert conflict_mod.conflict_penalty_mode() == "warn"
    assert conflict_mod.resolve_conflict_salience(conflicts) == 0
    assert _salience_of("tc-a") == pytest.approx(0.8)
    assert _salience_of("tc-b") == pytest.approx(0.8)
    # negative control: apply really does write, so the warn result is not vacuous
    assert conflict_mod.resolve_conflict_salience(conflicts, mode="apply") == 2
    assert _salience_of("tc-a") == pytest.approx(0.4)


def test_off_mode_via_env_changes_nothing(monkeypatch):
    monkeypatch.setenv("AIDUMEI_CONFLICT_PENALTY_MODE", "off")
    _insert([("tc-a", TRUE_CONFLICT[0]), ("tc-b", TRUE_CONFLICT[1])])
    assert conflict_mod.resolve_conflict_salience(conflict_mod.detect_conflicts()) == 0
    assert _salience_of("tc-a") == pytest.approx(0.8)


def test_apply_mode_via_env(monkeypatch):
    monkeypatch.setenv("AIDUMEI_CONFLICT_PENALTY_MODE", "apply")
    _insert([("tc-a", TRUE_CONFLICT[0]), ("tc-b", TRUE_CONFLICT[1])])
    assert conflict_mod.resolve_conflict_salience(conflict_mod.detect_conflicts()) == 2
    assert _salience_of("tc-b") == pytest.approx(0.4)


def test_invalid_mode_falls_back_to_warn_and_says_so(monkeypatch, caplog):
    monkeypatch.setenv("AIDUMEI_CONFLICT_PENALTY_MODE", "halve-everything")
    status = conflict_mod.conflict_penalty_mode_status()
    assert status["mode"] == "warn"
    assert status["error"] and "halve-everything" in status["error"]
    assert any("AIDUMEI_CONFLICT_PENALTY_MODE" in r.getMessage() for r in caplog.records)
    with pytest.raises(ValueError):
        conflict_mod.resolve_conflict_salience([], mode="halve-everything")


def test_apply_halves_each_memory_once_not_once_per_pair():
    hub = ("hub", HUB_TEXT)
    spokes = [(f"spoke-{i}", text) for i, text in enumerate(SPOKE_TEXTS)]
    _insert([hub] + spokes)
    conflicts = conflict_mod.detect_conflicts()
    hub_pairs = [c for c in conflicts if "hub" in (c["memory_a"], c["memory_b"])]
    assert len(hub_pairs) == len(SPOKE_TEXTS) >= 3
    assert conflict_mod.resolve_conflict_salience(conflicts, mode="apply") == 1 + len(SPOKE_TEXTS)
    assert _salience_of("hub") == pytest.approx(0.4), "x0.5 once per run, not x0.5**k"

    # negative control: the old per-pair loop compounds to 0.8 * 0.5**k
    conn = utils.get_salience_conn()
    conn.execute("UPDATE salience SET salience = 0.8")
    for c in hub_pairs:
        for mid in (c["memory_a"], c["memory_b"]):
            conn.execute("UPDATE salience SET salience = salience * 0.5 WHERE memory_id = ?", (mid,))
    conn.commit()
    assert _salience_of("hub") == pytest.approx(0.8 * 0.5 ** len(SPOKE_TEXTS))


# ---------------------------------------------------------------- comparison cap
def test_cap_truncates_and_reports(monkeypatch):
    _insert([(f"cap-{i}", text) for i, text in enumerate(SPOKE_TEXTS + [HUB_TEXT, TRUE_CONFLICT[0]])])
    n = len(SPOKE_TEXTS) + 2
    total = n * (n - 1) // 2
    small = conflict_mod.scan_conflicts(max_pairs=3)
    assert small["pairs_compared"] == 3 and small["pairs_total"] == total
    assert small["truncated"] is True and small["truncated_groups"] == 1
    # negative control: a cap at the real size is not truncated
    full = conflict_mod.scan_conflicts(max_pairs=total)
    assert full["pairs_compared"] == total and full["truncated"] is False
    # the env knob feeds the same cap
    monkeypatch.setenv("AIDUMEI_CONFLICT_MAX_PAIRS_PER_LANE", "4")
    assert conflict_mod.scan_conflicts()["pairs_compared"] == 4


def test_cap_env_garbage_falls_back_to_default(monkeypatch):
    for bad in ("abc", "nan", "0", "-5", "1.5"):
        monkeypatch.setenv("AIDUMEI_CONFLICT_MAX_PAIRS_PER_LANE", bad)
        assert conflict_mod.max_pairs_per_lane() == conflict_mod.DEFAULT_MAX_PAIRS_PER_LANE, bad


def test_cap_is_per_scope_and_lane_group():
    _insert([("g1-a", TRUE_CONFLICT[0]), ("g1-b", TRUE_CONFLICT[1])], lane="general")
    _insert([("g2-a", TRUE_CONFLICT[0]), ("g2-b", TRUE_CONFLICT[1])], lane="preference")
    scan = conflict_mod.scan_conflicts(max_pairs=1)
    assert scan["groups"] == 2 and scan["pairs_compared"] == 2 and scan["truncated"] is False
    assert _pairs(scan["conflicts"]) == {frozenset(("g1-a", "g1-b")), frozenset(("g2-a", "g2-b"))}

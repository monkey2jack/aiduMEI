"""v20.2.4 · WP-A 差异化时效衰减 + WP-B 纠正语检测 的验收门槛。

设计说明（给后来人）：计划书原写「变异探针：把表改成乱值，人工验红后还原」。
本文件把那些**一次性探针焊成常驻守卫** —— 用 monkeypatch 在测试内部制造变异，
于是「区分力真的来自那张表」「关闭时真没走这条路」每跑一次测试就验一次，
不靠下一个人记得去手工改文件。
"""
import ast
import time

import pytest

from ducky import scoring
from ducky.scoring import (
    RECENCY_LAMBDA,
    TYPE_DECAY,
    compute_time_decay,
    type_decay_lambda,
)

_ALL_TYPES = ("PREFERENCES", "DECISIONS", "FACTS", "REFLECTIONS", "EXPERIENCES", "OBSERVATIONS")


class TestThreshold1SwitchOffByteIdentical:
    """门槛 1 · 开关关闭时行为逐字节不变。"""

    def test_omitting_type_reproduces_legacy_value(self):
        now = time.time()
        for days in (1, 7, 30, 90, 365):
            ts = now - days * 86400
            legacy = compute_time_decay(ts, now, RECENCY_LAMBDA)
            assert compute_time_decay(ts, now, RECENCY_LAMBDA, memory_type=None) == legacy

    def test_switch_defaults_off(self, monkeypatch):
        monkeypatch.delenv("AIDUMEI_TYPE_DECAY", raising=False)
        assert scoring.type_decay_enabled() is False

    def test_switch_off_is_immune_to_table_mutation(self, monkeypatch):
        """常驻变异守卫：把表投毒，只要不传 memory_type，结果必须纹丝不动。

        它证明的是「关闭时真的没走这条路」——若将来有人把类型 λ 悄悄接进
        默认路径，本用例会立刻变红。
        """
        now = time.time()
        ts = now - 30 * 86400
        before = compute_time_decay(ts, now, RECENCY_LAMBDA)
        for k in _ALL_TYPES:
            monkeypatch.setitem(TYPE_DECAY, k, 99.0)
        assert compute_time_decay(ts, now, RECENCY_LAMBDA) == before


class TestThreshold2Discrimination:
    """门槛 2 · 开启后分档必须有区分力。"""

    # 30 天是实测出的有效窗口：λ=0.05 在 90 天后已饱和到 0，
    # 四个中高速档在长尺度上根本分不开——用一年做断言会得到「全 0 == 全 0」的假绿灯。
    _WINDOW_DAYS = 30

    def _spread(self):
        now = time.time()
        ts = now - self._WINDOW_DAYS * 86400
        vals = [compute_time_decay(ts, now, memory_type=m) for m in _ALL_TYPES]
        return max(vals) - min(vals)

    def test_preferences_far_outlives_observations(self):
        now = time.time()
        ts = now - self._WINDOW_DAYS * 86400
        pref = compute_time_decay(ts, now, memory_type="PREFERENCES")
        obs = compute_time_decay(ts, now, memory_type="OBSERVATIONS")
        assert pref - obs > 0.5, f"30 天尺度上偏好与观察必须显著拉开，实测 {pref:.4f} vs {obs:.4f}"

    def test_lambdas_are_monotonic_by_design(self):
        """六档必须严格单调——表被人随手改乱（比如把 OBSERVATIONS 调得比 FACTS 还慢）
        会让「分档」失去语义，而单看某一对的差值发现不了。"""
        lams = [TYPE_DECAY[m] for m in _ALL_TYPES]
        assert lams == sorted(lams), f"六档 λ 必须按 {_ALL_TYPES} 顺序单调不减，实测 {lams}"

    def test_flattening_the_table_kills_discrimination(self, monkeypatch):
        """反向探针（常驻）：六个 λ 全压成同一个值 → 区分力必须归零。

        它证明区分力**确实来自那张表**，而不是碰巧来自别处——否则本特性
        可能是个空转守卫：表在那儿，分数却由其他因素决定。
        """
        assert self._spread() > 0.5
        for k in _ALL_TYPES:
            monkeypatch.setitem(TYPE_DECAY, k, 0.05)
        assert self._spread() == pytest.approx(0.0, abs=1e-12)

    def test_perturbation_bounded_by_time_weight(self):
        """分档能造成的最大绝对分差 = w['time'] × 1.0，不得撼动 vector 主导。

        λ=0 的 PREFERENCES 永不衰减，在长尺度上时间分恒为满分。这条把它的
        影响力钉死在 0.15 分以内——与既有 1.35x 六型增益同量级，不是新的统治项。
        """
        w_time = scoring.DEFAULT_WEIGHTS["time"]
        assert w_time * 1.0 <= 0.15 + 1e-9
        assert w_time < scoring.DEFAULT_WEIGHTS["vector"], "时间分量不得超过向量分量"


class TestThreshold5SafeDegradation:
    """门槛 5 · 类型缺失时安全降级：不报错、不跳过、不给 0 分。"""

    @pytest.mark.parametrize("bad", [None, "", "UNKNOWN_XYZ", "facts_typo", 123, [], {}])
    def test_unknown_type_falls_back_to_global_lambda(self, bad):
        assert type_decay_lambda(bad) == RECENCY_LAMBDA

    def test_degraded_score_is_not_zero_and_matches_legacy(self):
        now = time.time()
        ts = now - 7 * 86400
        legacy = compute_time_decay(ts, now, RECENCY_LAMBDA)
        degraded = compute_time_decay(ts, now, memory_type="NO_SUCH_TYPE")
        assert degraded == legacy
        assert degraded > 0.0, "降级不得给 0 分——那等于把未分类的存量记忆全部枪毙"

    def test_case_insensitive(self):
        assert type_decay_lambda("preferences") == TYPE_DECAY["PREFERENCES"]

    def test_facts_lambda_equals_global_default(self):
        """FACTS 必须＝全局默认：上游 mtype 缺失时回退字面量 'FACTS'，
        这条等式是「未分类存量行为不变」的**唯一支点**，改了就破门槛 5。"""
        assert TYPE_DECAY["FACTS"] == RECENCY_LAMBDA


class TestThreshold3SingleBatchQuery:
    """门槛 3 · 类型查询不得逐条打库。"""

    def _run(self, monkeypatch, n=25):
        calls = {"count": 0, "sizes": []}
        import ducky.memory_types as mt

        def _spy(ids):
            calls["count"] += 1
            calls["sizes"].append(len(list(ids)))
            return {}

        monkeypatch.setattr(mt, "get_batch_memory_types", _spy)
        # rerank 走网络，测试里必须掐掉——否则本用例的耗时由外部服务决定
        import ducky.mem0_runtime as rt
        monkeypatch.setattr(rt, "rerank", lambda *a, **k: [], raising=False)

        now = time.time()
        cands = [
            {"id": f"m{i}", "memory": f"内容{i}", "created_at": now - i * 86400, "score": 0.5}
            for i in range(n)
        ]
        scoring.score_and_rank_candidates("测试查询", cands, limit=10)
        return calls

    def test_exactly_one_batch_call_regardless_of_candidate_count(self, monkeypatch):
        calls = self._run(monkeypatch, n=25)
        assert calls["count"] == 1, f"25 条候选必须只发 1 次类型查询，实测 {calls['count']} 次"
        assert calls["sizes"] == [25], f"必须一次带齐全部 id，实测 {calls['sizes']}"

    def test_switch_on_does_not_add_queries(self, monkeypatch):
        """承重对照：开启分档**不得**引入任何新的类型查询。

        本特性的立身之本就是「零新查询」——它只是把上游已经批量查好的类型
        用起来。若哪天有人图省事在循环里补一次查询，本用例变红。
        """
        monkeypatch.setenv("AIDUMEI_TYPE_DECAY", "1")
        calls = self._run(monkeypatch, n=25)
        assert calls["count"] == 1, f"开启分档后查询次数必须仍为 1，实测 {calls['count']}"


# ── 门槛 4 · 纠正语不许单独触发替换 ──

_MUTATING_SQL = ("UPDATE ", "DELETE ")


def _functions_calling(src: str, callee: str):
    """返回源码中所有「调用了 callee」的顶层/嵌套函数名。纯 AST 判据——
    绝不用字符串 grep：注释里提到函数名和真的调用它，grep 分不清（旧账）。"""
    tree = ast.parse(src)
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                name = getattr(f, "id", None) or getattr(f, "attr", None)
                if name == callee:
                    hits.append(fn)
                    break
    return hits


def _has_mutating_sql(node) -> bool:
    """子树里是否出现写库 SQL 字面量。"""
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if any(k in n.value.upper() for k in _MUTATING_SQL):
                return True
    return False


def _correction_gated_mutations(src: str) -> list:
    """找出「由纠正语把守的写库分支」——本守卫的真正判据。

    早期版本用的是粗判据「调用 is_correction 的函数不得含 UPDATE/DELETE」，
    它把**合法的标注用法**也判红（登记落在一个本来就要写库的函数里）。
    判据分不清「拿它做判决」和「拿它做标注」，就会逼着人绕过守卫——
    而绕过守卫比没有守卫更糟。

    现判据：is_correction 的返回值（含它绑定到的变量）不得出现在任何
    if / while / 三元表达式的**条件**里，而该分支体内执行写库。

    已知局限（如实写明）：只跟一层赋值传播；`x = signaled; if x:` 跟得住，
    `f(signaled)` 跟不住。这是判据的射程边界，不是它的失效。
    """
    tree = ast.parse(src)
    tainted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            if (getattr(f, "id", None) or getattr(f, "attr", None)) == "is_correction":
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        tainted.add(tgt.id)

    def _gated(test) -> bool:
        for n in ast.walk(test):
            if isinstance(n, ast.Name) and n.id in tainted:
                return True
            if isinstance(n, ast.Call):
                f = n.func
                if (getattr(f, "id", None) or getattr(f, "attr", None)) == "is_correction":
                    return True
        return False

    bad = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)) and _gated(node.test):
            for st in list(node.body) + list(node.orelse):
                if _has_mutating_sql(st):
                    bad.append(f"line {getattr(st, 'lineno', '?')}")
        elif isinstance(node, ast.IfExp) and _gated(node.test):
            for br in (node.body, node.orelse):
                if _has_mutating_sql(br):
                    bad.append(f"ifexp line {getattr(br, 'lineno', '?')}")
    return bad


class TestThreshold4CorrectionNeverDeletes:
    """门槛 4 · **承重那一半**：纠正语不得单独触发替换或删除。"""

    @pytest.mark.parametrize("s", [
        "不对，我生日是3月5日", "你记错了，是周三", "更正一下：项目改名了",
        "我改主意了，用方案B", "搞错了，不是那个人", "Actually, it is 42",
        "Correction: the port changed", "No, it is the other one",
        "嗯，说错了，应该是明天", "That's not right",
    ])
    def test_positive_samples(self, s):
        from ducky.conflict_resolver import is_correction
        assert is_correction(s) is True

    @pytest.mark.parametrize("s", [
        "这个方案不对称", "我没错过任何一次", "结构不对等会有问题",
        "他从没搞错过", "actually working on it", "时间不对劲吗",
        "错过了末班车", "对不对都行", "这话没毛病", "没搞错的话是这样",
    ])
    def test_negative_samples_zero_false_positive(self, s):
        """误报对照是承重的：本谓词若将来被接进判决路径，一次误报＝用户
        随口一句就丢记忆，且不可逆。"""
        from ducky.conflict_resolver import is_correction
        assert is_correction(s) is False, f"误报：{s!r} 不是纠正语"

    def test_predicate_is_pure(self):
        from ducky.conflict_resolver import is_correction
        for bad in (None, "", 0, [], {}):
            assert is_correction(bad) is False

    def test_correction_never_gates_a_write(self):
        """红线守卫：纠正语的返回值不得把守任何写库分支。

        v20.2.4 起 is_correction **确实**被接进了 scan_and_resolve_text_conflicts
        —— 但只用于日志与账本标注，判决分支一个字没碰。本用例就是这句话的
        机器判据：它允许标注，禁止判决。
        """
        import ducky.conflict_resolver as cr
        src = open(cr.__file__, encoding="utf-8").read()
        bad = _correction_gated_mutations(src)
        assert not bad, f"纠正语把守了写库分支（{bad}）—— 红线：不得单独触发替换或删除"

    def test_guard_catches_a_real_violation(self):
        """射程自证：守卫必须真抓得住违规样本，否则「扫过了」与「扫得到」
        看起来一模一样（S-2 教训）。"""
        violating = (
            "def resolve(text):\n"
            "    signaled = is_correction(text)\n"
            "    if signaled:\n"
            "        cur.execute('UPDATE facts SET valid_to = ?', [now])\n"
        )
        assert _correction_gated_mutations(violating), "守卫漏掉了人造违规样本"

        inline = (
            "def resolve(text):\n"
            "    if is_correction(text):\n"
            "        cur.execute('DELETE FROM facts WHERE id = ?', [1])\n"
        )
        assert _correction_gated_mutations(inline), "守卫漏掉了内联调用形态"

    def test_guard_permits_legitimate_annotation(self):
        """负向对照（承重）：守卫必须**放行**合法的标注用法。

        一律喊红的守卫和从不喊红的守卫一样没用——前者还会逼着人绕过它。
        这条钉死它的区分力：同一个函数里既有写库、又有纠正语标注，
        只要标注没把守写库分支，就必须绿。
        """
        legit = (
            "def resolve(text):\n"
            "    signaled = is_correction(text)\n"
            "    if not rules:\n"
            "        if signaled:\n"
            "            logger.info('just logging')\n"
            "        return []\n"
            "    for r in rows:\n"
            "        cur.execute('UPDATE facts SET valid_to = ?', [now])\n"
            "    note = ' marked' if signaled else ''\n"
        )
        assert not _correction_gated_mutations(legit), (
            "守卫误伤了合法标注用法——它分不清判决与标注"
        )

    def test_redline_comment_survives(self):
        """红线注释存在性：那段注释是本设计唯一的口头约束载体，删了就没了。"""
        import ducky.conflict_resolver as cr
        src = open(cr.__file__, encoding="utf-8").read()
        assert "绝不允许单独触发替换或删除" in src, "WP-B 红线注释被删——请勿删除它"


class TestWiringEndToEnd:
    """接线守卫。

    上面的门槛 2 直接调纯函数，**守不住接线**——若有人把 scoring 里那句
    `memory_type=mtype if _type_decay_on else None` 改回常量 None，门槛 2 照样全绿，
    而特性实际已经死了。本类从 score_and_rank_candidates 端到端进出，钉死接线。
    """

    def _score(self, monkeypatch, on: bool):
        import ducky.memory_types as mt
        import ducky.mem0_runtime as rt
        types = {"m0": "PREFERENCES", "m1": "OBSERVATIONS"}
        monkeypatch.setattr(mt, "get_batch_memory_types", lambda ids: types)
        monkeypatch.setattr(rt, "rerank", lambda *a, **k: [], raising=False)
        if on:
            monkeypatch.setenv("AIDUMEI_TYPE_DECAY", "1")
            assert scoring.type_decay_enabled() is True, "前提反证：开关没真开，本用例会假绿"
        else:
            monkeypatch.delenv("AIDUMEI_TYPE_DECAY", raising=False)
            assert scoring.type_decay_enabled() is False

        now = time.time()
        aged = now - 30 * 86400          # 同龄，只有类型不同
        cands = [
            {"id": "m0", "memory": "偏好记忆", "created_at": aged, "score": 0.5},
            {"id": "m1", "memory": "观察记忆", "created_at": aged, "score": 0.5},
        ]
        out = scoring.score_and_rank_candidates("查询", cands, limit=10)
        return {it["id"]: it["_time_decay"] for it in out}

    def test_wiring_alive_when_on(self, monkeypatch):
        d = self._score(monkeypatch, on=True)
        assert d["m0"] > d["m1"] + 0.5, (
            f"接线断了：同龄的偏好与观察时间分应显著拉开，实测 {d}"
        )

    def test_wiring_silent_when_off(self, monkeypatch):
        d = self._score(monkeypatch, on=False)
        assert d["m0"] == d["m1"], f"开关关闭时同龄记忆必须同分，实测 {d}"


# ── WP-B · B1「只登记不判决」的落地验收 ──

class TestCorrectionIsLoggedNeverJudged:
    """纠正语登记必须**完全不改变判决**。

    本类的承重用例是那条对照：同一段会触发规则的文本，带纠正语与不带纠正语
    消解的行数必须**一模一样**。它是「只登记不判决」这句话唯一的硬证据。
    """

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path, monkeypatch):
        import sqlite3
        import ducky.utils as utils
        db = str(tmp_path / "facts.db")
        monkeypatch.setattr(utils, "FACTS_DB", db)
        monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "fts.db"))
        # 两道护栏：断言测试库确实在临时目录里，绝不碰仓库 data/
        assert str(tmp_path) in db and "/data/" not in db
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "category TEXT DEFAULT 'general', fact_key TEXT NOT NULL, fact_value TEXT NOT NULL, "
            "source TEXT DEFAULT 'local', agent_id TEXT DEFAULT 'local', archived INTEGER DEFAULT 0, "
            "valid_to TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, "
            "user_id TEXT, bank_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE fact_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, "
            "category TEXT, fact_key TEXT, new_value TEXT, affected_ids TEXT, created_at TEXT, "
            "user_id TEXT, bank_id TEXT)"
        )
        conn.commit()
        conn.close()
        self.db = db

    def _seed(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO facts (category, fact_key, fact_value, user_id, bank_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("config", "调试开关", "调试开关已开启", "u1", "b1"),
        )
        conn.commit()
        conn.close()

    def _events(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT new_value FROM fact_events").fetchall()
        conn.close()
        return [r[0] for r in rows]

    # 「关闭」命中互斥规则的 new_re，库里的「开启」命中 old_re → 真实消解
    PLAIN = "调试开关现在关闭了"
    WITH_CORRECTION = "不对，调试开关现在关闭了"

    def test_correction_does_not_change_the_verdict(self):
        """承重：带不带纠正语，消解结果必须逐字相同。"""
        import ducky.conflict_resolver as cr
        self._seed()
        plain = cr.scan_and_resolve_text_conflicts(self.PLAIN, user_id="u1", bank_id="b1")

        # 重新播种同样的一行，跑带纠正语的版本
        self._seed()
        marked = cr.scan_and_resolve_text_conflicts(self.WITH_CORRECTION, user_id="u1", bank_id="b1")

        assert len(plain) == 1, "前提反证：不带纠正语时本该消解 1 行，否则本对照没有区分力"
        assert len(marked) == len(plain), (
            f"纠正语改变了判决！不带={len(plain)} 带={len(marked)} —— 违反红线"
        )
        assert [a["fact_key"] for a in marked] == [a["fact_key"] for a in plain]

    def test_correction_with_no_rule_hit_deletes_nothing(self, caplog):
        """最有价值也最危险的场景：用户明说记错了，而内容层无规则接得住。
        必须**零消解**，只留一条日志。"""
        import logging
        import sqlite3
        import ducky.conflict_resolver as cr
        self._seed()
        with caplog.at_level(logging.INFO, logger="aiduMEM.ConflictResolver"):
            out = cr.scan_and_resolve_text_conflicts("不对，你记错了", user_id="u1", bank_id="b1")
        assert out == [], "纠正语单独触发了消解 —— 这是本设计最不能犯的错"

        conn = sqlite3.connect(self.db)
        alive = conn.execute("SELECT COUNT(*) FROM facts WHERE valid_to IS NULL").fetchone()[0]
        n_events = conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0]
        conn.close()
        assert alive == 1, "记忆被纠正语弄失效了"
        assert n_events == 0, "无消解时不该写账本（那会为记账而打库）"
        assert "仅登记不消解" in caplog.text, "信号被吞了，登记没落地"

    def test_marker_present_only_when_signaled(self):
        """账本标记的正负对照 —— 只在真有纠正语时出现。"""
        import ducky.conflict_resolver as cr
        self._seed()
        cr.scan_and_resolve_text_conflicts(self.WITH_CORRECTION, user_id="u1", bank_id="b1")
        assert any("user_signaled_correction" in e for e in self._events())

    def test_marker_absent_without_signal(self):
        import ducky.conflict_resolver as cr
        self._seed()
        cr.scan_and_resolve_text_conflicts(self.PLAIN, user_id="u1", bank_id="b1")
        evs = self._events()
        assert evs, "前提反证：本该写出一条账本行"
        assert not any("user_signaled_correction" in e for e in evs), "无纠正语却打了标记"

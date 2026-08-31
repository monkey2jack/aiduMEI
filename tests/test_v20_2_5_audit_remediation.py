"""v20.2.5 两份审计整改的验收门槛（用户用户视角 + 第三方外审）。

A1 这一组的存在理由要写在最前面：v20.2.4 声称修了「refine 候选跨 bank」
（马院士 F-10），实际是**算出了 SQL 子句却一个字都没拼进 SELECT**，而注释写着
已修、结案陈词列为已修、**且没有任何测试盯着这条**。外审 F-03 用一个四行的
SQLite 复现就把它翻出来了。

所以这组用例的判据全部是**集合相等**，不是「有没有返回」——后者对
「bank_b 的行也混进来了」这种缺陷毫无区分力。
"""
import ast
import os
import pathlib
import re
import sqlite3
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _make_facts_db(path, *, with_bank: bool = True):
    """建一个 facts 库 —— **用生产的 DDL 与生产的迁移函数，不手写**。

    第一版这里是手写的 CREATE TABLE，结果一路缺列：先缺 row_factory，
    再缺 `archived_at`（apply 归档时要写它），错误还长得像产品缺陷
    （`no such column`）。手写替身注定追不上生产的 24 列 —— 而且它每缺一列，
    就多一个「测试里过不了、生产里其实好的」假红灯，或者反过来。

    所以：基础表走 `schema_bootstrap._FACTS_DDL`（生产同一份 DDL），
    scope 列走 `bank_contract.ensure_memory_banks_schema`（生产同一条迁移）。
    `with_bank=False` 只跑第一步 —— 那正是 v19 老库的真实形态，不是我编的形态。
    """
    from ducky.schema_bootstrap import _FACTS_DDL
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_FACTS_DDL)
    if with_bank:
        from ducky.bank_contract import ensure_memory_banks_schema
        ensure_memory_banks_schema(conn)
    conn.commit()
    return conn


class TestA1RefineCandidatesScoped:
    """A1 · 外审 F-03：候选不许跨 bank。"""

    @pytest.fixture
    def conn(self, tmp_path):
        c = _make_facts_db(tmp_path / "facts.db")
        # 外审原始复现数据：bank_a 放 A、C；bank_b 放 B、D
        c.executemany(
            "INSERT INTO facts (category, fact_key, fact_value, source, user_id, bank_id)"
            " VALUES ('general', ?, ?, ?, ?, ?)",
            [("kA", "A", "alice", "alice", "bank_a"),
             ("kC", "C", "alice", "alice", "bank_a"),
             ("kB", "B", "alice", "alice", "bank_b"),
             ("kD", "D", "alice", "alice", "bank_b")],
        )
        c.commit()
        yield c
        c.close()

    def test_default_identity_branch_is_bank_scoped(self, conn):
        """默认身份分支：取 bank_a 的候选必须**恰好**是 {A, C}。

        判据是集合相等 —— 「返回了 A 和 C」对「B、D 也混进来了」没有区分力，
        而那正是 F-03 的形态（外审实测返回 A、C、B、D 四条）。
        """
        from ducky.refine_memory import _load_candidates
        got = {r["fact_value"] for r in _load_candidates(conn, "default", "general", 50, bank_id="bank_a")}
        assert got == {"A", "C"}, f"候选跨了 bank：{sorted(got)}（期望恰好 A、C）"

        other = {r["fact_value"] for r in _load_candidates(conn, "default", "general", 50, bank_id="bank_b")}
        assert other == {"B", "D"}, f"另一侧也要对称成立：{sorted(other)}"

    def test_named_tenant_branch_is_bank_scoped(self, conn):
        """具名租户分支：此前**连 bank 参数都没用到**，只按 source 收身份轴。"""
        from ducky.refine_memory import _load_candidates
        got = {r["fact_value"] for r in _load_candidates(conn, "alice", "general", 50, bank_id="bank_a")}
        assert got == {"A", "C"}, f"具名租户分支跨了 bank：{sorted(got)}"

    def test_bank_clause_is_actually_spliced_into_sql(self):
        """结构判据：算出来的子句必须**真的**进了 SQL 字符串。

        F-03 的根因是「算了不用」—— 一个纯 AST/文本层面就能钉死的形态。
        判据落在「f-string 里出现了 {_bclause}」+「参数里出现了 *_bparams」，
        两者缺一都说明那半截又断了。
        """
        src = (_ROOT / "ducky" / "refine_memory.py").read_text(encoding="utf-8")
        body = src[src.index("def _load_candidates"):src.index("def _parse_refine_json")]
        assert body.count("_bclause}") == 2, (
            "两个分支的 SELECT 里都必须拼上 {_bclause}（F-03 就是漏了这一步）"
        )
        assert body.count("*_bparams") == 2, "两个分支都必须把 _bparams 展开进参数元组"

    def test_legacy_schema_degrades_without_narrowing(self, tmp_path):
        """负向对照：v19 老库没有 bank_id 列时**不收窄、不报错**。

        这条防的是「为了修 F-03 把老库打成 0 条候选」——本文件上方 v19.4.2
        注释记着那种事故：refine 静默退化，返回一句合法的 skipped。
        """
        from ducky.refine_memory import _bank_clause, _load_candidates
        c = _make_facts_db(tmp_path / "legacy.db", with_bank=False)
        c.executemany(
            "INSERT INTO facts (category, fact_key, fact_value, source) VALUES ('general', ?, ?, 'alice')",
            [("k1", "L1"), ("k2", "L2")],
        )
        c.commit()
        clause, params = _bank_clause(c, "bank_a")
        assert (clause, params) == ("", []), "老库上不该收窄"
        got = {r["fact_value"] for r in _load_candidates(c, "default", "general", 50, bank_id="bank_a")}
        assert got == {"L1", "L2"}, f"老库候选被打没了：{sorted(got)}"
        c.close()


class TestA1AppliedSummaryScope:
    """A1 · 外审 F-03/F-04 摘要错域：apply 出来的摘要行必须落在**账本记的那个库**。

    这个类是变异探针逼出来的：上面四条用例全绿的时候，把 `owner_bank` 改回硬写
    `DEFAULT_BANK_ID`（也就是 v20.2.4 的原样）**测试照样全绿** —— 说明「摘要继承
    bank」这条修复当时没有任何测试盯着。**这正是 F-03 得以存在的同一个机制：
    改了代码、没有判据。** 探针不跑就发现不了。
    """

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        import ducky.utils as utils
        path = str(tmp_path / "facts.db")
        monkeypatch.setattr(utils, "FACTS_DB", path)
        monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "fts.db"))
        assert str(tmp_path) in path and "/data/" not in path      # 两道护栏
        c = _make_facts_db(path)
        c.execute(
            "CREATE TABLE refined_memories (refine_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT NOT NULL DEFAULT 'default', bank_id TEXT NOT NULL DEFAULT 'default',"
            " category TEXT NOT NULL DEFAULT 'general', source_ids TEXT NOT NULL,"
            " summary TEXT NOT NULL, reason TEXT DEFAULT '', confidence REAL DEFAULT 0.5,"
            " state TEXT DEFAULT 'proposed', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            " applied_at TIMESTAMP)"
        )
        c.executemany(
            "INSERT INTO facts (category, fact_key, fact_value, source, user_id, bank_id)"
            " VALUES ('general', ?, ?, 'alice', 'alice', 'work')",
            [("s1", "旧事实一"), ("s2", "旧事实二")],
        )
        # 账本行记的是 alice/work
        c.execute(
            "INSERT INTO refined_memories (user_id, bank_id, category, source_ids, summary, state)"
            " VALUES ('alice', 'work', 'general', '[1, 2]', '合并后的摘要', 'proposed')"
        )
        c.commit(); c.close()
        return path

    def test_summary_row_inherits_ledger_bank(self, db):
        """外审实测：账本记 alice/work，apply 后摘要行落 alice/**default**。"""
        from ducky.refine_memory import apply_refinement
        res = apply_refinement(1, user_id="alice", bank_id="work")
        assert res.get("status") in ("ok", "applied"), f"apply 未成功：{res}"

        conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT user_id, bank_id FROM facts WHERE fact_key='refined:1'"
        ).fetchone()
        conn.close()
        assert row is not None, "摘要行没写进 facts"
        assert (row["user_id"], row["bank_id"]) == ("alice", "work"), (
            f"摘要落错域：{(row['user_id'], row['bank_id'])} —— 期望 ('alice', 'work')。"
            "账本行记的 bank 必须被继承，不许硬写默认域"
        )


# ════════════════════════════════════════════════════════════════════
# A5 · Ruff 真缺陷类规则（外审工具面）
# ════════════════════════════════════════════════════════════════════
#
# 由来：Ruff **此前根本没装**。而 F-03 那个「算出 SQL 子句却没拼进 SELECT」
# 正是 F841 一秒能抓的形态；F821 抓到的 `_os` 未定义更狠 —— 那行一执行就
# NameError，被外层 except 吞掉，于是那段读 manifest 的配置逻辑**从来没成功
# 执行过**，一直走兜底值。工具能抓的错，不该靠人眼和运气。

_RUFF_TARGETS = ["ducky/", "api_server.py", "mcp_server.py", "scripts/", "conftest.py"]
_F841_BASELINE = 9        # 存量「算了不用」；新增请优先修，确实要留就改这个数并写明理由


def ruff_available() -> bool:
    """ruff 在不在场 —— **闸门与跳过轴探测器共用的唯一判据**（v20.2.5）。

    抽成公开函数是因为普查那边的探测器必须问闸门本身。v20 有过前车之鉴：
    探测器自己另写一套判据（硬查一个写死的路径），闸门收紧后两边射程不同，
    于是探测器报「齐备」而同一轮里那条轴实实在在门控掉了 12 条用例。
    判据只留一份，就不存在「两边不同步」这种失效方式。
    """
    import importlib.util
    return importlib.util.find_spec("ruff") is not None


def _ruff(rules: str) -> list:
    """跑 ruff 并返回命中行。**工具不在就跳过，绝不静默当成「无命中」。**

    这条修正是沙箱实测逼出来的：生产 venv 没装 ruff，第一版实现直接返回空
    列表 —— 于是 F821/F811 那条守卫在工具缺失的环境里**永远通过**。
    一个守卫在依赖缺失时静默变绿，比没有守卫更危险：它让「扫过了」和
    「扫不动」看起来一模一样（S-2 那条教训的又一种形态）。

    判据用**模块探测**而不是退出码：第一版写的是「returncode not in (0,1) 就
    跳过」，而 `python -m ruff` 在模块缺失时返回码**也是 1** —— 与「有命中」
    撞在一起，区分不开。这个漏洞是「把 ruff 目录藏起来再跑」这条负向对照
    当场抓出来的：期望 2 skipped，实得 1 failed。**判据必须有区分力。**
    """
    if not ruff_available():
        # 轴标识（`ruff 不可用`）必须落在 `pytest.skip(` 这一行上：
        # 跳过轴普查按**原始行**认领跳过点，标识跑到续行去就没人认领它。
        pytest.skip("ruff 不可用（本环境未装 dev 依赖）—— "
                    "静态关在 push_gate 侧仍然拦，这里诚实跳过而不是假装通过")
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *_RUFF_TARGETS,
         "--select", rules, "--output-format", "concise"],
        cwd=_ROOT, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode in (0, 1), (
        f"ruff 装着却跑不起来（rc={proc.returncode}）：{proc.stderr.strip()[:200]}"
    )
    return [ln for ln in proc.stdout.splitlines() if re.search(r":\d+:\d+: [A-Z]+\d+", ln)]


def test_no_undefined_names_or_redefinitions():
    """F821/F811 必须零命中 —— 这两类是**运行时会炸**的形态。

    push_gate 的第四道关也拦这两条，但那只在推送前跑；套件里也要有一份，
    否则「本地全绿」和「能推」之间又出现一道缝。
    """
    hits = _ruff("F821,F811")
    assert not hits, (
        "F821（未定义名）/F811（重复定义）必须零命中：\n  " + "\n  ".join(hits)
        + "\n\nF821 的典型后果：那行一执行就 NameError，若被 except 吞掉，"
          "整段逻辑会静默失效而请求照常返回 200。"
    )


def test_unused_locals_stay_on_baseline():
    """F841「算了不用」走登记制。

    为什么不直接设成零：存量 10 处里混着无害残留（`results = []` 初始化后未用）
    和**疑似真缺陷**（`recall_funnel.py:140` 查了库、建好 lane_map、然后丢掉 ——
    白查一次库）。一次全清会淹没真信号，也会牵进 Lethe 子系统，不属本版范围。
    所以钉住数量：**新增一条就红**，逼作者当场判断它是残留还是又一个 F-03。
    """
    hits = _ruff("F841")
    assert len(hits) == _F841_BASELINE, (
        f"F841 条数 {len(hits)} ≠ 登记基线 {_F841_BASELINE}：\n  "
        + "\n  ".join(hits[:12])
        + f"\n\n新增了？先判断是无害残留还是「算了该用没拼上」（F-03 的形态）。"
          f"确实要留就把 _F841_BASELINE 改到 {len(hits)} 并在 CHANGELOG 说明。"
    )


# ════════════════════════════════════════════════════════════════════
# A2 · 删除结果三态（外审 F-02）
# ════════════════════════════════════════════════════════════════════
#
# 外审的复现只有四行：把 `_delete_scoped_vectors` 换成必抛异常的桩，
# `cascade_delete_all` 照样返回 `status="ok"`、WAL pending 归零。
# 于是「部分删除」和「完整删除」在调用方看来一模一样 —— 合规擦除、
# 用户恢复、事故响应全都失去证据链。
#
# 出口那一端还断了第二次：HTTP 层硬编码 `{"status": "ok"}`，把底层判决整个
# 抹平，连 v20.2.4 加的 `not_cleared` 都从没到达过调用方。所以这组用例
# **两端都测**：判决逻辑一端，HTTP 出口一端。

class TestA2DeleteOutcomeStates:

    def test_critical_layer_failure_is_not_success(self, tmp_path, monkeypatch):
        """核心层（向量）失败 → 必须是 failed，且该层出现在 failed_layers 里。

        这是外审原始探针的直接复刻。
        """
        import ducky.utils as utils
        monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
        monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "fts.db"))
        assert str(tmp_path) in utils.FACTS_DB and "/data/" not in utils.FACTS_DB

        import ducky.mem0_runtime as rt
        import ducky.wal_engine as we

        # 后端必须**在场**：判据看的是异常来源 —— 取不到后端算「未启用」，
        # 拿到后端却删失败才算真失败。本用例复刻的正是后者（生产上 Qdrant
        # 在、删除炸了），所以两个都要 mock。
        monkeypatch.setattr(rt, "get_memory", lambda: object())

        def _boom(*a, **kw):
            raise RuntimeError("vector enumeration exploded (probe)")

        monkeypatch.setattr(we, "_delete_scoped_vectors", _boom)
        out = we.cascade_delete_all("probe_user", bank_id="probe_bank", confirm=True)

        assert out["status"] != "ok", "status 还叫 ok —— F-02 的原形态"
        assert out["status"] in ("partial", "failed"), f"未知状态：{out['status']}"
        layers = {f["layer"] for f in out.get("failed_layers", [])}
        assert "mem0_vectors" in layers, f"失败层没被记下来：{layers}"
        assert out["status"] == "failed", "向量层是核心层，失败必须判 failed 而非 partial"

    def test_failed_layers_and_not_cleared_are_separate_fields(self, tmp_path, monkeypatch):
        """`failed_layers`（本次实际失败）与 `not_cleared`（矩阵预声明豁免）
        必须是两个字段，且失败层不许混进豁免清单。

        混在一起会让 not_cleared 加重误导：调用方以为它就是「全部没清的东西」。
        """
        import ducky.utils as utils
        monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "f.db"))
        monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "t.db"))
        import ducky.mem0_runtime as rt
        import ducky.wal_engine as we
        monkeypatch.setattr(rt, "get_memory", lambda: object())
        monkeypatch.setattr(we, "_delete_scoped_vectors",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("probe")))
        out = we.cascade_delete_all("probe2", bank_id="b", confirm=True)

        assert "failed_layers" in out and "not_cleared" in out
        failed = {f["layer"] for f in out["failed_layers"]}
        exempt = {str(x) for x in out["not_cleared"]}
        assert not (failed & exempt), f"失败层混进了豁免清单：{failed & exempt}"

    @pytest.mark.parametrize("outcome,expected_code", [
        ("committed", 200),
        ("partial", 207),      # 强制调用方注意到「不是完全成功」
        ("failed", 500),
    ])
    def test_http_status_follows_business_outcome(self, outcome, expected_code, monkeypatch):
        """HTTP 出口必须跟着业务状态走。

        外审门槛的原话：「注入任意一层故障，HTTP 状态与业务状态必须显式失败」。
        200 + 一个藏在 body 里的字段做不到这一点 —— 调用方只判 status_code 时
        照样把部分删除当成全删。
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import ducky.hot.crud as crud

        monkeypatch.setattr(crud, "cascade_delete_all", lambda **kw: {
            "status": outcome,
            "details": {"facts_deleted": 3},
            "failed_layers": [] if outcome == "committed" else [{"layer": "fts", "error_type": "RuntimeError"}],
            "not_cleared": [{"store": "checkpoints", "reason": "exempt"}],
        })
        app = FastAPI()
        crud.register_crud_routes(app)
        resp = TestClient(app).post("/delete_all", json={"user_id": "u", "confirm": True})

        assert resp.status_code == expected_code, (
            f"outcome={outcome} 应映射到 HTTP {expected_code}，实得 {resp.status_code}")
        body = resp.json()
        assert body["status"] == outcome, "业务状态被抹平了（F-02 出口端的原形态）"
        assert "not_cleared" in body, "not_cleared 没透传 —— v20.2.4 那条修复就是断在这里"

    def test_absent_backend_is_not_counted_as_failure(self, tmp_path, monkeypatch):
        """判据的另一半：**取不到后端**不算删除失败。

        与上一条构成对照 —— 同样是向量层出问题，但一个是「这个部署没启用
        向量库」，一个是「向量库在、删不掉」。判据若只看层名或配置文件，
        两者就会被混成一种，那就是 F-02 的原病：生产上后端一断，删除报成功。
        """
        import ducky.utils as utils
        monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "f.db"))
        monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "t.db"))
        import ducky.mem0_runtime as rt
        import ducky.wal_engine as we
        monkeypatch.setattr(rt, "get_memory",
                            lambda: (_ for _ in ()).throw(RuntimeError("mem0 不可用: 未配置")))
        out = we.cascade_delete_all("probe3", bank_id="b", confirm=True)
        layers = {f["layer"] for f in out.get("failed_layers", [])}
        assert "mem0_vectors" not in layers, (
            f"后端不在场被算成了删除失败：{out.get('failed_layers')}")


# ════════════════════════════════════════════════════════════════════
# A3 · 运行目录交接（外审 F-01，P0）
# ════════════════════════════════════════════════════════════════════
#
# 外审的实测：wheel 装进 site-packages 后，`BASE_DIR` 由 `__file__` 上两级推导，
# 于是 `DATA_DIR = site-packages/data` —— 而 Docker bind-mount 的是 /app/data。
# 两者不一致**没有任何症状**：服务正常起、接口正常答，数据写进容器层、重建即丢；
# site-packages 只读时首次写入直接 `attempt to write a readonly database`。
#
# 这几条都跑**子进程**：DATA_DIR 是 import 期求值的模块级常量，父进程里
# monkeypatch 环境变量影响不到它 —— 那样的测试会稳过，且证明不了任何事
# （v20.2.3 的配置雷用例踩过同一个坑）。

class TestA3RuntimeDirs:

    def _probe(self, env_extra: dict) -> dict:
        import json
        env = {k: v for k, v in os.environ.items()
               if k not in ("AIDUMEM_HOME", "AIDUMEM_DATA_DIR", "AIDUMEM_LOG_DIR")}
        env.update(env_extra)
        code = (
            "import json, os;"
            "from ducky.utils import BASE_DIR, DATA_DIR, LOG_DIR;"
            "print(json.dumps({'base': BASE_DIR, 'data': DATA_DIR, 'log': LOG_DIR}))"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, env=env,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, f"子进程失败：{out.stderr[-400:]}"
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_explicit_env_vars_win(self, tmp_path):
        """三个环境变量必须真的能改写运行目录 —— Docker/systemd 模板靠它们交接。"""
        d, l = tmp_path / "d", tmp_path / "l"
        got = self._probe({"AIDUMEM_DATA_DIR": str(d), "AIDUMEM_LOG_DIR": str(l)})
        assert got["data"] == str(d), f"AIDUMEM_DATA_DIR 没生效：{got}"
        assert got["log"] == str(l), f"AIDUMEM_LOG_DIR 没生效：{got}"

    def test_home_var_relocates_both(self, tmp_path):
        """只设 AIDUMEM_HOME 时，data/logs 都跟着走 —— 这是最省事的交接方式。"""
        got = self._probe({"AIDUMEM_HOME": str(tmp_path)})
        assert got["base"] == str(tmp_path)
        assert got["data"] == str(tmp_path / "data")
        assert got["log"] == str(tmp_path / "logs")

    def test_delivery_templates_hand_over_runtime_dirs(self):
        """Dockerfile 与 systemd 模板必须**显式**交接运行目录。

        这条守的是外审 F-01 的根因：变量支持一直都在（utils.py 早就读
        AIDUMEM_HOME/DATA_DIR/LOG_DIR），**缺的是交付模板没设**。
        代码能力与交付形态之间那道缝，只有这种对表能盯住。
        """
        needed = ("AIDUMEM_HOME", "AIDUMEM_DATA_DIR", "AIDUMEM_LOG_DIR", "AIDUMEM_CONFIG_FILE")
        for f in ("Dockerfile", "deploy/aidumem-api.service"):
            src = (_ROOT / f).read_text(encoding="utf-8")
            missing = [v for v in needed if v not in src]
            assert not missing, f"{f} 未交接运行目录：{missing}"

    def test_health_reports_actual_paths(self):
        """/health 必须报出实际打开的目录 —— 「以为挂载生效了其实没有」只能靠它。"""
        src = (_ROOT / "ducky" / "hot" / "health.py").read_text(encoding="utf-8")
        for key in ("runtime_paths", "data_dir_writable", "data_dir_inside_package"):
            assert key in src, f"/health 缺少运行目录探针字段：{key}"


# ══════════════════════════════════════════════════════════════════════════
# v20.2.5-b：生产实机冒烟抓出的两条（D1 句柄删不掉、D2 单条删除没有三态）
#
# 这一组存在的理由要写清楚：**改契约的时候整套 1460 条一条都没红。**
# 也就是说，单条删除的返回状态**从来没有任何测试盯着**——
# 与 F-03 那笔假账、与 Ruff 守卫的假绿灯，是同一种缺席。
# ══════════════════════════════════════════════════════════════════════════

class _FakeMem0:
    """mem0 替身。**签名逐字对齐生产调用点**（F-15 的教训）：

    `_scoped_vector_items` 先试 `get_all(filters=…, top_k=…)`，
    `TypeError` 才退到 `limit=`。替身若只接 `*a, **kw`，就永远走不到那条
    回退分支、也测不出参数名写错——**替身比生产宽，缺陷就隐形**。
    """

    def __init__(self, items):
        self._items = list(items)
        self.deleted = []

    def get_all(self, filters=None, top_k=None, **kw):
        return {"results": list(self._items)}

    def delete(self, mid):
        self.deleted.append(str(mid))

    def delete_all(self, *a, **kw):        # pragma: no cover —— 调到即失败
        raise AssertionError("单条删除绝不许触发无作用域 delete_all")


def _raw_items(content_hash, vector_id, bank="default"):
    """一条 `/add/raw` 写出来的向量点：id 是 mem0 自己铸的 UUID，
    与调用方手上的 `raw-…` 句柄**没有任何字面关系**，只靠 content_hash 相连。
    """
    return [{
        "id": vector_id,
        "memory": "原文正文",
        "metadata": {"bank_id": bank, "content_hash": content_hash,
                     "memory_tier": "verbatim"},
    }]


def test_raw_handle_maps_to_its_content_hash():
    """`raw-<hash>-<rand>` → `<hash>`；其它形态一律返回空串。

    负向对照就在同一条用例里：`verbatim:1` / 裸 UUID / 空值都必须返回 ""，
    否则这个换算会把不相干的 id 也当成 raw 句柄去反查。
    """
    from ducky.wal_engine import _raw_handle_hash

    h = "0123456789abcdef"
    assert _raw_handle_hash(f"raw-{h}-deadbeef") == h
    assert _raw_handle_hash(f"raw-{h.upper()}-deadbeef") == h, "大小写要归一"
    for other in ("verbatim:12", "496065f9-26cd-4105-8183-b563a29f9e6b",
                  "raw-短-x", "", None, "rawnodash"):
        assert _raw_handle_hash(other) == "", f"{other!r} 不该被当成 raw 句柄"


def test_raw_handle_delete_actually_removes_the_vector(monkeypatch):
    """D1 的判据：拿 `/add/raw` 返回的句柄删，**mem0 那个点必须真的被删掉**。

    实机形态：`DELETE /delete?memory_id=raw-00632d1f1383` 回 200 `{"status":"ok"}`，
    而向量、facts、fts 全留着、原文照旧可召回。根因是这一层只比 id ——
    而 `/add/raw` 走 `mem.add(infer=False)`，mem0 自己铸 UUID。

    判据是**删掉的 id 集合相等**，不是「调用过 delete」：后者对「删错了别的点」
    毫无区分力，而删错点是不可挽回的。
    """
    from ducky import wal_engine

    h = "00632d1f1383abcd"
    vid = "496065f9-26cd-4105-8183-b563a29f9e6b"
    fake = _FakeMem0(_raw_items(h, vid))
    monkeypatch.setattr("ducky.mem0_runtime.get_memory", lambda: fake)

    out = wal_engine.cascade_delete_memory(f"raw-{h}-deadbeef",
                                          user_id="t_raw_d1", bank_id="default")
    assert fake.deleted == [vid], (
        f"该删的是 mem0 的 UUID {vid}，实际删了 {fake.deleted} —— "
        "句柄没被解析（D1 原形），或者解析出了不该删的点"
    )
    assert out["details"]["mem0_vector"] is True
    assert out["details"]["raw_handle_resolved_ids"] == [vid]
    assert out["status"] in ("committed", "partial"), out


def test_raw_handle_delete_does_not_touch_other_banks(monkeypatch):
    """反查必须留在域内：同一个 content_hash 在别的域里也有点时，不许连坐。

    没有这条，上一条用例对「反查越域」是零区分力的 —— 而越域删除不可挽回。
    """
    from ducky import wal_engine

    h = "00632d1f1383abcd"
    mine, theirs = "vec-mine", "vec-theirs"
    fake = _FakeMem0(_raw_items(h, mine, bank="default")
                     + _raw_items(h, theirs, bank="work"))
    monkeypatch.setattr("ducky.mem0_runtime.get_memory", lambda: fake)

    wal_engine.cascade_delete_memory(f"raw-{h}-deadbeef",
                                     user_id="t_raw_bank", bank_id="default")
    assert fake.deleted == [mine], f"只该删本域那个点，实际 {fake.deleted}"


def test_single_delete_reports_three_states_and_both_lists(monkeypatch):
    """D2 的判据：单条删除返回三态 + `failed_layers` + `not_cleared`。

    改这个契约时，整套 1460 条**一条都没红** —— 单条删除的状态从来没人盯。
    """
    from ducky import wal_engine

    h = "abcdefabcdef0011"
    vid = "vec-3state"

    # ① 全绿 → committed，两个清单都在
    fake = _FakeMem0(_raw_items(h, vid))
    monkeypatch.setattr("ducky.mem0_runtime.get_memory", lambda: fake)
    ok = wal_engine.cascade_delete_memory(f"raw-{h}-00000001",
                                          user_id="t_3s_ok", bank_id="default")
    assert ok["status"] == "committed", ok
    assert ok["failed_layers"] == [], ok
    assert "not_cleared" in ok, "矩阵预声明豁免必须到达调用方（F-02 的另一半）"
    assert ok["details"]["matched"] is True

    # ② 关键层抛异常 → failed（不是 ok，也不是 partial）
    class _Boom(_FakeMem0):
        def delete(self, mid):
            raise RuntimeError("向量后端连不上（模拟）")

    boom = _Boom(_raw_items(h, vid))
    monkeypatch.setattr("ducky.mem0_runtime.get_memory", lambda: boom)
    bad = wal_engine.cascade_delete_memory(f"raw-{h}-00000002",
                                           user_id="t_3s_bad", bank_id="default")
    assert bad["status"] == "failed", bad
    assert {f["layer"] for f in bad["failed_layers"]} == {"mem0_vector"}, bad


def test_enumeration_failure_is_a_failure_not_a_success(monkeypatch):
    """向量枚举失败 ≠「没有这条」。**它是「问不出来」，必须算失败。**

    这正是 F-02 的原病：生产 Qdrant 一断，删除报成功。
    """
    from ducky import wal_engine

    class _Blind(_FakeMem0):
        def get_all(self, filters=None, top_k=None, **kw):
            raise RuntimeError("向量后端不可用（模拟）")

    blind = _Blind([])
    monkeypatch.setattr("ducky.mem0_runtime.get_memory", lambda: blind)
    out = wal_engine.cascade_delete_memory("some-uuid-1234",
                                           user_id="t_blind", bank_id="default")
    assert out["status"] == "failed", out
    assert {f["layer"] for f in out["failed_layers"]} == {"mem0_vector"}, out
    assert blind.deleted == [], "归属没确认就不许删"


def test_nothing_matched_says_not_found_instead_of_ok(monkeypatch):
    """一层都没命中时，状态必须是 `not_found` —— **D1 当初就藏在那句 ok 底下**。

    判据基于「真删掉了几个」，不是「SQL 跑过了」：`details["fts"]` 是布尔
    「执行过」，拿它判命中会把「跑了但 0 行」算成命中，守卫立刻变白护栏。
    """
    from ducky import wal_engine

    fake = _FakeMem0([])          # 域内什么都没有
    monkeypatch.setattr("ducky.mem0_runtime.get_memory", lambda: fake)
    out = wal_engine.cascade_delete_memory("raw-ffffffffffffffff-99999999",
                                           user_id="t_none", bank_id="default")
    assert out["status"] == "not_found", out
    assert out["details"]["matched"] is False, out
    assert fake.deleted == []


def test_every_handle_form_the_product_mints_has_a_delete_path():
    """**P5 守卫**：产品铸出来发给调用方的每一种 id 形态，删除链都要认得。

    D1 的根因不是某一行写错，是 v19.4.1 修 `verbatim:` 句柄时**只加了一个
    前缀分支，没问「还有哪些句柄形态」**。所以这里把问题问成守卫：扫源码里
    「给 memory_id 赋一个带字面前缀的值」的位点，逐个要求删除链有对应处理。
    下一种句柄进来时，这条会红。
    """
    minted = {}
    for path in sorted((_ROOT / "ducky").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "memory_id" not in names:
                continue
            if not isinstance(node.value, ast.JoinedStr):
                continue
            head = node.value.values[0] if node.value.values else None
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                m = re.match(r"^([A-Za-z_]+[-:])", head.value)
                if m:
                    minted[m.group(1)] = f"{path.name}:{node.lineno}"

    engine = (_ROOT / "ducky" / "wal_engine.py").read_text(encoding="utf-8")
    unhandled = {}
    for prefix, where in minted.items():
        bare = prefix.rstrip("-:")
        # 删除链认得它 = 有前缀分支，或有专门的换算函数
        if re.search(rf'startswith\(["\']{re.escape(prefix)}', engine):
            continue
        if re.search(rf'_{bare}_handle_hash|r"\^{re.escape(bare)}-', engine):
            continue
        unhandled[prefix] = where
    assert not unhandled, (
        f"这些句柄形态产品发得出去，删除链却不认：{unhandled} —— "
        "拿它删会一层都命不中而回一句成功（D1 原形）"
    )
    assert "raw-" in minted, (
        "扫不到 raw- 这个位点了 —— 要么铸法改了（守卫失去着力点），"
        "要么 /add/raw 没了（那 README 得跟着改）"
    )


def test_raw_handle_delete_removes_the_facts_row(tmp_path, monkeypatch):
    """D3 的判据：`/add/raw` 登记的 facts 行必须被同一次删除带走。

    实机形态：即使换成正确的 mem0 UUID 删成功，`details["facts"]` 仍是 **0**。
    因为 `/add/raw` 落的键是 `raw:<content_hash>`（raw_drawer.py），
    而删除链拼的是 `raw:<完整句柄>` —— 与库里的键永远差一截。

    判据是 rowcount 与**留下来的行**两头都验：只看「删了 1 行」对
    「顺手删了别人的行」没有区分力。
    """
    from ducky import wal_engine

    h = "112233445566aabb"
    db = tmp_path / "facts.db"
    conn = _make_facts_db(db)
    conn.execute(
        """INSERT INTO facts (category, fact_key, fact_value, source,
                              memory_tier, agent_id, user_id, bank_id)
           VALUES (?,?,?,?,'verbatim',?,?,?)""",
        ("verbatim", f"raw:{h}", "原文正文", "raw_drawer",
         "t_facts_d3", "t_facts_d3", "default"))
    # 负向对照行：同一个租户、同一个域，键不同 —— 绝不许被连坐删掉
    conn.execute(
        """INSERT INTO facts (category, fact_key, fact_value, source,
                              memory_tier, agent_id, user_id, bank_id)
           VALUES (?,?,?,?,'verbatim',?,?,?)""",
        ("verbatim", "raw:ffffffffffffffff", "别人的原文", "raw_drawer",
         "t_facts_d3", "t_facts_d3", "default"))
    conn.commit()
    conn.close()

    monkeypatch.setattr("ducky.wal_engine.get_facts_conn",
                        lambda: sqlite3.connect(db))
    fake = _FakeMem0([])
    monkeypatch.setattr("ducky.mem0_runtime.get_memory", lambda: fake)

    out = wal_engine.cascade_delete_memory(f"raw-{h}-0badc0de",
                                          user_id="t_facts_d3", bank_id="default")
    assert out["details"]["facts"] == 1, (
        f'facts 应删掉 1 行，实际 {out["details"]["facts"]} —— '
        "键形没换算（D3 原形）"
    )
    left = sqlite3.connect(db)
    keys = {r[0] for r in left.execute("SELECT fact_key FROM facts")}
    left.close()
    assert keys == {"raw:ffffffffffffffff"}, (
        f"剩下的键应当只有那条对照行，实际 {keys} —— 删多了或删少了"
    )


def test_delete_local_counts_what_it_removed_not_what_was_asked():
    """`delete_local` 必须报**真删掉了几个**，不是**请求了几个**。

    生产实测抓到：删一个从来不存在的 id 也回 1（原实现 `return len(point_ids)`），
    于是 `local_vector_deleted` 恒为真，把「一层都没命中」的判据整个作废 ——
    单条删除对不存在的 id 仍报 `committed`。

    同一个模块里的 `delete_local_by_scope` 一直是对的（先 count 再删、返回 count）。
    **两个孪生函数，一个诚实一个不诚实** —— 这条把不诚实那个钉住。

    替身实现 `retrieve` 是**对齐生产 API 面**：新版 qdrant-client 有它，
    生产走的就是那条路。替身少一个方法，测的就是回落分支而不是主路径。
    """
    from ducky import dual_index

    class _Col:
        def __init__(self, names):
            self.collections = [type("C", (), {"name": n})() for n in names]

    class _FakeQ:
        def __init__(self,存在):
            self.存在 = set(存在)
            self.deleted = []

        def get_collections(self):
            return _Col([dual_index.LOCAL_COLLECTION])

        def retrieve(self, collection_name, ids, **kw):
            return [type("P", (), {"id": i})() for i in ids if str(i) in self.存在]

        def delete(self, collection_name, points_selector):
            self.deleted.extend(str(p) for p in points_selector)

    q = _FakeQ({"pid-real"})
    assert dual_index.delete_local(["pid-real"], client=q) == 1
    # 负向对照：不存在的 id 必须回 0 —— 这一条才是判据的区分力所在
    assert dual_index.delete_local(["pid-ghost"], client=q) == 0, (
        "删一个不存在的点回了非 0 —— 报的是请求数而不是删除数（原缺陷形态）"
    )
    assert dual_index.delete_local(["pid-real", "pid-ghost"], client=q) == 1
    # 删除本身仍要发出去（少删一个点比多报一个数字严重得多）
    assert q.deleted == ["pid-real", "pid-ghost", "pid-real", "pid-ghost"]

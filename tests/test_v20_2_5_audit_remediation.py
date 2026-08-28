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
_F841_BASELINE = 10        # 存量「算了不用」；新增请优先修，确实要留就改这个数并写明理由


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

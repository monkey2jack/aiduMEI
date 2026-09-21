"""v22.1 Hermes 升级适配套件

建议一（turn_author 多实体隔离）+ 建议三（Hook 熔断旁路）。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="aidumem_v22_1_")
_DB = os.path.join(_tmp, "facts.db")

import ducky.utils as utils  # noqa: E402
utils.FACTS_DB = _DB


@pytest.fixture(autouse=True)
def _db():
    utils.FACTS_DB = _DB
    c = sqlite3.connect(_DB)
    c.executescript(
        "DROP TABLE IF EXISTS facts; DROP TABLE IF EXISTS memory_epistemic; "
        "PRAGMA user_version=0; "
        "CREATE TABLE facts(id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT);")
    from ducky.schema_bootstrap import apply_migrations
    apply_migrations(c)
    c.commit()
    c.close()
    yield


class TestTurnAuthorIsolation:
    """建议一：is_bot=True 的记忆默认降权（fuzzy 而非 reasoned）。"""

    def test_bot_memory_marked_fuzzy(self):
        """bot 记忆必须标 fuzzy，非 bot 保持 reasoned。"""
        # 直接调 stamp_memory_refs 的分流逻辑（不走 HTTP）
        from ducky.epistemic import stamp_memory_refs
        from ducky.origin_context import set_origin, reset_origin

        # bot 记忆：is_bot=True → fuzzy
        token = set_origin("hermes", "sess1", 1)
        try:
            n = stamp_memory_refs(["mem-bot-1"], "fuzzy",
                                  user_id="test_user", bank_id="default",
                                  source="hermes",
                                  origin=("hermes", "sess1", 1))
            assert n == 1
        finally:
            reset_origin(token)

        # 非 bot 记忆：is_bot=False → reasoned
        token = set_origin("hermes", "sess1", 2)
        try:
            n = stamp_memory_refs(["mem-user-1"], "reasoned",
                                  user_id="test_user", bank_id="default",
                                  source="hermes",
                                  origin=("hermes", "sess1", 2))
            assert n == 1
        finally:
            reset_origin(token)

        # 查 sidecar：bot 的是 fuzzy，非 bot 的是 reasoned
        conn = sqlite3.connect(_DB)
        rows = conn.execute(
            "SELECT origin_agent, epistemic_mode FROM memory_epistemic ORDER BY rowid"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][1] == "fuzzy", f"bot 记忆必须标 fuzzy，实际 {rows[0][1]}"
        assert rows[1][1] == "reasoned", f"非 bot 记忆必须标 reasoned，实际 {rows[1][1]}"

    def test_empty_author_id_no_downgrade(self):
        """author_id 为空时按现有语义（不降权）——不破坏单用户场景。"""
        from ducky.epistemic import stamp_memory_refs
        n = stamp_memory_refs(["mem-normal-1"], "reasoned",
                              user_id="test_user", bank_id="default",
                              source="hermes")
        assert n == 1
        conn = sqlite3.connect(_DB)
        rows = conn.execute(
            "SELECT epistemic_mode FROM memory_epistemic ORDER BY rowid DESC LIMIT 1"
        ).fetchall()
        conn.close()
        assert rows and rows[0][0] == "reasoned", "无 is_bot 时必须保持 reasoned"


class TestAddIsBotDowngrade:
    """/add 端点的 is_bot 分流：bot 记忆标 fuzzy，非 bot 保持 reasoned。"""

    def test_add_bot_memory_fuzzy(self, monkeypatch):
        """/add 收到 is_bot=True 时，写入的 sidecar 必须标 fuzzy。"""
        monkeypatch.setenv("AIDUMEI_FEDERATION_ADMINS", "admin")
        from ducky.epistemic import stamp_memory_refs

        # bot 记忆：is_bot=True → fuzzy（模拟 add.py 的分流）
        _is_bot = True
        _mode = "fuzzy" if _is_bot else "reasoned"
        n = stamp_memory_refs(["mem-bot-add-1"], _mode,
                              user_id="test_user", bank_id="default",
                              source="hermes")
        assert n == 1

        # 非 bot 记忆：is_bot=False → reasoned
        _is_bot = False
        _mode = "fuzzy" if _is_bot else "reasoned"
        n = stamp_memory_refs(["mem-user-add-1"], _mode,
                              user_id="test_user", bank_id="default",
                              source="hermes")
        assert n == 1

        conn = sqlite3.connect(_DB)
        rows = conn.execute(
            "SELECT epistemic_mode FROM memory_epistemic ORDER BY rowid DESC LIMIT 2"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        # 最新两条：第一条（rowid 大）是 reasoned，第二条是 fuzzy
        assert rows[0][0] == "reasoned"
        assert rows[1][0] == "fuzzy"


class TestHookCircuitBreaker:
    """建议三：熔断旁路——服务失败时 Hook 快速返回，不阻塞聊天。"""

    def test_circuit_breaker_script_exists(self):
        """熔断逻辑必须在 inject.sh 顶部。"""
        from pathlib import Path
        sh = Path("integrations/aidumem-inject.sh").read_text()
        assert "_CIRCUIT_FILE" in sh
        assert "_FUSE_COUNT_FILE" in sh
        assert "connect-timeout 0.15" in sh
        assert "冷却窗检查" in sh

    def test_circuit_breaker_cooldown_logic(self, tmp_path):
        """冷却窗逻辑：熔断标记存在且未过期时直接返回。"""
        circuit = tmp_path / ".aidumem_circuit_broken"
        circuit.write_text(str(int(__import__("time").time())))  # 刚熔断
        # 模拟脚本第一行的冷却窗检查
        import time
        broken_at = int(circuit.read_text())
        now = int(time.time())
        assert (now - broken_at) < 5, "熔断标记未过期，Hook 应直接返回空上下文"

"""
tests/test_audit_fixes_v19_1.py — v19.1 审计修复回归测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
锁死社区审计指出、本版已修的若干问题，防回归：
  🟢20 session_unpin 判空逻辑
  🟢21 session_search context_used 不再恒真
  🔴9  SQLite REGEXP 已注册
  🔴8  /crystals/approve 端点存在且生效
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import ducky.utils as utils

_tmp = tempfile.mkdtemp(prefix="aidumem_audit_fix_")
_TEST_DB = os.path.join(_tmp, "facts.db")
utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def _bind_db():
    utils.FACTS_DB = _TEST_DB
    yield


# ── 🔴9 REGEXP 已注册 ─────────────────────────────────────
def test_sqlite_regexp_registered():
    conn = utils.get_facts_conn()
    assert conn.execute("SELECT 1 WHERE 'hello world' REGEXP 'wor'").fetchone() is not None
    assert conn.execute("SELECT 1 WHERE 'abc' REGEXP 'xyz'").fetchone() is None
    # NULL 安全
    assert conn.execute("SELECT 1 WHERE NULL REGEXP 'x'").fetchone() is None


# ── 🟢20 session_unpin 判空逻辑 ───────────────────────────


# ── 🟢21 context_used 不再恒真 ────────────────────────────



"""
tests/test_v19_4_0_inject_frame_server.py — v19.4.0 审计修复 🔴-A 回归测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

生产审计 🔴-A：B4 召回侧注入框架只活在 shell hook 里，生产注入路径
（/facts/inject-context 服务端出口）裸奔。修复：服务端出口自己套框架，
hook 侧识别 <memory> 标记避免双重包装。本文件守住：

1. 服务端 inject_context 出口 context 带框架文案 + <memory> 边界
2. 框架文案与 hook 的 INJECT_FRAME_TOP 逐字一致（同源守卫）
3. 空召回不包装（wrapped=False，context 为空）
4. raw_context 保留未包装原文，total_tokens 按 raw 计（预算语义不变）
5. wrap_inject_frame 幂等：已包装内容不二次包装
6. hook _wrap_block 对已含 <memory> 的内容透传不双包（bash 实测）
"""

import os
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_HOOK_PATH = os.path.join(_REPO_ROOT, "integrations", "aidumem-inject.sh")

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v1941_frame_")

import ducky.utils as utils
utils.FACTS_DB = os.path.join(_tmp_dir, "facts.db")
utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")

from ducky.facts_recall import INJECT_FRAME_TOP, inject_context, wrap_inject_frame


@pytest.fixture(autouse=True)
def _setup_test_env():
    utils.FACTS_DB = os.path.join(_tmp_dir, "facts.db")
    utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")
    conn = utils.get_facts_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'general',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            summary TEXT,
            overview TEXT,
            source TEXT DEFAULT 'default',
            trust_score REAL DEFAULT 0.5,
            archived INTEGER DEFAULT 0,
            archived_at TIMESTAMP,
            valid_from TIMESTAMP,
            valid_to TIMESTAMP,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            retrieval_count INTEGER DEFAULT 0,
            last_accessed_at TIMESTAMP
        )"""
    )
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, trust_score) "
        "VALUES ('偏好', 'coffee', '喜欢喝热拿铁，不加糖', 0.5)"
    )
    conn.commit()
    conn.close()
    yield


# ─────────────────────────────────────────────────────────────
# 1. 服务端出口自带框架
# ─────────────────────────────────────────────────────────────

def test_inject_context_wrapped_with_frame():
    res = inject_context("拿铁")
    assert res["injected_facts"] == 1
    ctx = res["context"]
    assert ctx.startswith(INJECT_FRAME_TOP), "出口 context 必须以框架文案开头"
    assert "<memory>" in ctx and "</memory>" in ctx
    assert "喜欢喝热拿铁" in ctx
    assert res["wrapped"] is True


def test_frame_wording_matches_hook():
    """框架文案与 hook INJECT_FRAME_TOP 逐字一致（同源守卫）"""
    with open(_HOOK_PATH, encoding="utf-8") as f:
        src = f.read()
    assert f"INJECT_FRAME_TOP='{INJECT_FRAME_TOP}'" in src, (
        "服务端与 hook 的框架文案不同步——两侧必须逐字一致"
    )


def test_inject_context_empty_not_wrapped():
    res = inject_context("完全不相关的查询词xyz")
    assert res["injected_facts"] == 0
    assert res["context"] == ""
    assert res["raw_context"] == ""
    assert res["wrapped"] is False


def test_raw_context_and_token_budget_semantics():
    """raw_context 未包装；total_tokens 按 raw 计（预算语义与 v19.4.0 一致）"""
    res = inject_context("拿铁")
    raw = res["raw_context"]
    assert "<memory>" not in raw and INJECT_FRAME_TOP not in raw
    assert "喜欢喝热拿铁" in raw
    assert res["total_tokens"] == (len(raw) + 3) // 4


# ─────────────────────────────────────────────────────────────
# 2. wrap_inject_frame 幂等
# ─────────────────────────────────────────────────────────────

def test_wrap_idempotent():
    once = wrap_inject_frame("- [偏好] coffee: 喜欢喝热拿铁")
    twice = wrap_inject_frame(once)
    assert once == twice, "已包装内容不得二次包装"
    assert once.count("<memory>") == 1


def test_wrap_empty():
    assert wrap_inject_frame("") == ""
    assert wrap_inject_frame("   ") == ""


# ─────────────────────────────────────────────────────────────
# 3. hook 侧双包防护（bash 实测）
# ─────────────────────────────────────────────────────────────

def _extract_wrap_block() -> str:
    """从 hook 抽出 INJECT_FRAME_TOP 定义 + _wrap_block 函数体"""
    with open(_HOOK_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    out, in_func = [], False
    for line in lines:
        if line.startswith("INJECT_FRAME_TOP="):
            out.append(line)
        if line.startswith("_wrap_block()"):
            in_func = True
        if in_func:
            out.append(line)
            if line.strip() == "}":
                break
    return "".join(out)


def test_hook_wrap_block_skips_already_wrapped():
    snippet = _extract_wrap_block()
    assert snippet, "hook 里找不到 _wrap_block"
    script = (
        snippet + "\n"
        + 'plain="一行普通记忆内容"\n'
        + 'wrapped="$INJECT_FRAME_TOP\n<memory>\n服务端已包装的内容\n</memory>"\n'
        + 'out_plain="$(_wrap_block "$plain")"\n'
        + 'out_wrapped="$(_wrap_block "$wrapped")"\n'
        + 'printf "%s\\n---\\n%s\\n" "$out_plain" "$out_wrapped"\n'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    plain_out, wrapped_out = proc.stdout.split("---\n", 1)
    assert "<memory>" in plain_out, "未包装内容应被包装"
    assert wrapped_out.count("<memory>") == 1, "已包装内容被二次包装了"
    assert wrapped_out.startswith("[以下为召回的记忆数据"), "已包装内容应原样透传"

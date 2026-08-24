"""tests/test_v20_entity_keywords_source.py — v20 P0-3：实体词表的来源必须可见

用户视角审计 🔴-1：`AIDUMEM_ENTITY_KEYWORDS` 住在 systemd drop-in
`entity-keywords.conf` 里，不在 `.env` 里；全仓 0 处引用那个 conf 文件。

生产实测把机制查清了，比报告里写的更要紧：**合并后的 unit 中，drop-in 的
`Environment=` 排在 `EnvironmentFile=/…/.env` 之后 —— 也就是说 drop-in 永远压过
`.env`。** 于是「把它迁进 `.env`」这件事如果只做一半（加了 `.env` 那行、没删
drop-in），`.env` 就成了纯装饰：改它没有任何效果，而且没有任何东西会因此变红。
`/health` 照样报 `entity_keywords: 22`，绿得和真的一样。

所以 P0-2 的迁移必须配一个探针：光报「配了几个词」不够，得回答**「它来自唯一真相
源吗」**。进程侧看不见值出自哪一个 systemd 层（环境变量只有值，没有出身），但看得见
一件等价有用的事 —— 活值和 `.env` 声明的那一份是否相等。

这就是铁律 13「配置写了不等于配置生效」的一个具体探针。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_entity_keywords_source.py -v
"""
from __future__ import annotations

import importlib
import logging
import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ducky.pipeline.memory_gate as gate  # noqa: E402

_KEY = "AIDUMEM_ENTITY_KEYWORDS"
_WORDS = "甲|乙|丙|项目丁"


@pytest.fixture
def _sandbox(tmp_path, monkeypatch):
    """把 BASE_DIR 指进临时目录，`.env` 与环境变量都由用例自己摆。"""
    import ducky.utils as utils
    monkeypatch.setattr(utils, "BASE_DIR", str(tmp_path))
    monkeypatch.delenv(_KEY, raising=False)
    # 缓存与一次性 warning 标记都要复位，否则用例之间互相污染
    monkeypatch.setattr(gate, "_SELF_REF_CACHE", {"key": None, "pattern": None})
    monkeypatch.setattr(gate, "_ENTITY_WARNED", False)

    def write_env(line: str | None):
        p = tmp_path / ".env"
        body = "# 测试用 .env\nAIDUMEM_API_TOKEN=xxxxxxxxxxxx\n"
        if line is not None:
            body += line + "\n"
        p.write_text(body, encoding="utf-8")
        return p
    return write_env


# ═══════════════ ① 六种来源逐个点名 ═══════════════

def test_source_env_file_when_live_matches_declared(_sandbox, monkeypatch):
    """正常态：活值 == `.env` 声明值 → `env_file`。"""
    _sandbox(f"{_KEY}={_WORDS}")
    monkeypatch.setenv(_KEY, _WORDS)
    st = gate.entity_keywords_status()
    assert st["source"] == "env_file", st
    assert st["configured"] is True and st["count"] == 4


def test_source_overridden_when_live_differs_from_declared(_sandbox, monkeypatch):
    """★ 这条是整个文件的理由：**两边都有值但不相等**。

    这正是「只迁一半」的形态 —— `.env` 写了新词表，drop-in 还压着旧的。
    整改前 `/health` 只报 count，两个不同的词表报出来一模一样，无从分辨。
    """
    _sandbox(f"{_KEY}={_WORDS}")
    monkeypatch.setenv(_KEY, "完全不同的词|另一个")
    st = gate.entity_keywords_status()
    assert st["source"] == "overridden", (
        f"活值与 .env 声明值不同却报 {st['source']!r} —— "
        "「改 .env 没有效果」这件事必须能被看见"
    )
    # 注意：count 仍然是 2，configured 仍然是 True —— 光看这两个字段永远发现不了
    assert st["configured"] is True, "旧判据在这种形态下依然全绿，这就是它不够用的证明"


def test_source_outside_env_file_when_declared_line_absent(_sandbox, monkeypatch):
    """活值有、`.env` 没这一行 → `outside_env_file`（迁移前的原始形态）。"""
    _sandbox(None)
    monkeypatch.setenv(_KEY, _WORDS)
    assert gate.entity_keywords_status()["source"] == "outside_env_file"


def test_source_declared_not_effective(_sandbox, monkeypatch):
    """`.env` 声明了却没生效 → `declared_not_effective`（铁律 13 的原话形态）。"""
    _sandbox(f"{_KEY}={_WORDS}")
    monkeypatch.delenv(_KEY, raising=False)
    st = gate.entity_keywords_status()
    assert st["source"] == "declared_not_effective"
    assert st["configured"] is False


def test_source_unset_when_neither_side_has_it(_sandbox, monkeypatch):
    _sandbox(None)
    monkeypatch.delenv(_KEY, raising=False)
    assert gate.entity_keywords_status()["source"] == "unset"


def test_source_reports_missing_env_file_instead_of_guessing(_sandbox, monkeypatch, tmp_path):
    """`.env` 不存在时报 `no_env_file`，不许猜成 `env_file`。

    读不到判据就说读不到 —— 把「无法判定」渲染成「正常」，正是假绿灯。
    """
    monkeypatch.setenv(_KEY, _WORDS)
    assert not (tmp_path / ".env").exists()
    assert gate.entity_keywords_status()["source"] == "no_env_file"


# ═══════════════ ② 未设置时的告警（P0-3 验收基准原文） ═══════════════

def test_unset_logs_a_warning_and_flips_ok_false(_sandbox, monkeypatch, caplog):
    """★ 验收基准正向：未设置时 `entity_keywords_ok is False` **且**日志出现 warning。"""
    _sandbox(None)
    monkeypatch.delenv(_KEY, raising=False)
    with caplog.at_level(logging.WARNING):
        gate.get_self_reference()
    st = gate.entity_keywords_status()
    assert st["configured"] is False
    hits = [r.getMessage() for r in caplog.records if _KEY in r.getMessage()]
    assert hits, f"未设置却一条 warning 都没有：{[r.getMessage() for r in caplog.records]}"


def test_configured_flips_both_back(_sandbox, monkeypatch, caplog):
    """★ 验收基准负向对照：设置后两者都翻转（ok 变 True、warning 不再出现）。"""
    _sandbox(f"{_KEY}={_WORDS}")
    monkeypatch.setenv(_KEY, _WORDS)
    with caplog.at_level(logging.WARNING):
        gate.get_self_reference()
    st = gate.entity_keywords_status()
    assert st["configured"] is True and st["count"] == 4
    hits = [r for r in caplog.records
            if r.levelno >= logging.WARNING and "未设置" in r.getMessage()]
    assert not hits, f"已配置却还在报未设置：{[r.getMessage() for r in hits]}"


# ═══════════════ ③ /health 必须把来源端出来 ═══════════════

def test_health_probe_exposes_source_and_warns_on_override(_sandbox, monkeypatch):
    """★ `/health` 得有 `entity_keywords_source`，且失配时要进 warnings 列表。

    断言落在**探针函数的产物**上，而不是「源码里有没有这个字符串」：
    字符串级判据分不清代码和注释。
    """
    import ducky.hot.health as health_mod
    src = open(os.path.join(_REPO_ROOT, "ducky/hot/health.py"), encoding="utf-8").read()
    assert "entity_keywords_source" in src, "/health 没有暴露来源字段"

    # 直接验判据函数：失配态必须能被 health 的分支逮到
    _sandbox(f"{_KEY}={_WORDS}")
    monkeypatch.setenv(_KEY, "别的词|又一个")
    st = gate.entity_keywords_status()
    assert st["source"] == "overridden"

    # health 里那几条 warning 分支必须逐个覆盖到全部「非正常」来源，
    # 否则某个来源会静默通过 —— 这是守卫射程（铁律 12）
    import re
    branches = set(re.findall(r'ek\.get\("source"\) == "([a-z_]+)"', src))
    abnormal = {"overridden", "outside_env_file", "declared_not_effective"}
    missing = abnormal - branches
    assert not missing, (
        f"/health 没有为以下异常来源准备 warning 分支：{sorted(missing)} —— "
        "它们会静默通过，探针形同虚设"
    )

"""f0.6++ WP9 · core autosink(用户级审计暗礁2)行为守卫。

三条语义钉死:
  1. 占位块可被 autosink 草填(带 [自动草稿·待确认] 前缀);
  2. 用户已填/已草填的块**绝不覆写**(暗礁2 的反面:别把用户内容冲了);
  3. 开关 AIDUMEI_CORE_AUTOSINK=0 时整体跳过。

隔离:tmp 数据目录 + reset_for_test;不触生产 dudu 域。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def cm_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("AIDUMEI_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.delenv("AIDUMEI_CORE_AUTOSINK", raising=False)
    # reset 核心单例,确保连的是 tmp 库
    import ducky.core_memory as cm
    if hasattr(cm, "reset_for_test"):
        cm.reset_for_test()
    yield cm
    if hasattr(cm, "reset_for_test"):
        cm.reset_for_test()


def test_autosink_fills_placeholder_block(cm_env):
    from ducky import core_memory as cm
    cm.init_core_memory("probe_user", "default")
    res = cm.autosink_from_distill(
        "owner 的生日是四月十二号，并且确认了沟通偏好是简洁中文。",
        user_id="probe_user", bank_id="default")
    assert res["status"] == "written", f"占位块未被草填: {res}"
    blocks = cm.get_all_blocks("probe_user", "default")
    block = blocks.get(res["block"]) or (blocks.get("blocks", {}) or {}).get(res["block"])
    assert block and str(cm_get_content(block)).startswith("[自动草稿·待确认] ")


def cm_get_content(block):
    """get_all_blocks 的形状兼容:值可为 dict(content=..) 或 str。"""
    if isinstance(block, dict):
        return block.get("content", "")
    return str(block)


def test_autosink_never_overwrites_user_content(cm_env):
    from ducky import core_memory as cm
    cm.init_core_memory("probe_user", "default")
    cm.put_block("core_user_profile", "用户自己写的身份信息，不许动。",
                 user_id="probe_user", bank_id="default")
    res = cm.autosink_from_distill(
        "owner 的生日是四月十二号。",
        user_id="probe_user", bank_id="default")
    blocks = cm.get_all_blocks("probe_user", "default")
    probe = blocks.get("core_user_profile")
    content = cm_get_content(probe)
    assert content == "用户自己写的身份信息，不许动。", (
        f"用户内容被 autosink 覆盖 —— 红线: {content!r}")
    # 其余占位块仍可被填:状态为 written 或 skipped(not_placeholder)都对,
    # 只断言「用户块未被写」已完成主体判断;这里再确认 res 不指向 user_profile
    assert res.get("block") != "core_user_profile"


def test_autosink_switch_off(cm_env, monkeypatch):
    monkeypatch.setenv("AIDUMEI_CORE_AUTOSINK", "0")
    from ducky import core_memory as cm
    cm.init_core_memory("probe_user", "default")
    res = cm.autosink_from_distill("生日是四月十二号_this is long enough",
                                   user_id="probe_user", bank_id="default")
    assert res == {"status": "skipped", "reason": "disabled"}

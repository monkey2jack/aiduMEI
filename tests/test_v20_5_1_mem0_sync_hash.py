"""
tests/test_v20_5_1_mem0_sync_hash.py — T-16：mem0_sync 去重键 md5 → sha256
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
mem0_sync.py:56 的 `hashlib.md5` 是 MEMORY.md 条目的**本地去重键**，持久化在
`.sync_state.json`（{hash: memory_id}）。md5 在 2026 年不该再出现在任何
新写入路径上 —— 哪怕这里不是抗碰撞场景，留着它会让每一次安全扫描与审计
都要重新论证一遍「这个 md5 无害」。

持久键换算法的兼容处置（本文件钉死，防回流）：
  · 同一内容的新旧哈希必然不同，裸换算法 = 存量状态键全部失配 =
    升级后首次同步把整本 MEMORY.md **重推一遍**（服务端 /add 判重兜底前，
    网络与 LLM 抽取开销已经花掉）。
  · 处置：sync_once 装载状态后做一次**幂等迁移** —— 旧算法（md5）我们是
    已知的，对当前 MEMORY.md 还在的条目逐条重算新旧键映射，原值平移；
    对不上当前内容的键属于已删除条目的历史残留，丢弃（它们的去重义务
    随条目消失而终结；若原文日后回归，代价是重推一条，可接受）。
  · 迁移对新格式状态是恒等操作，无需版本标记，无条件每跑一次。

判据（先红后绿）：实现前 1/3/4/6 红（算法未换、迁移不存在）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mem0_sync  # noqa: E402


def _md5_12(text: str) -> str:
    """旧算法的独立复算（不 import 生产实现，防「测试和被测一起改错」）。"""
    return hashlib.md5(text.strip().encode()).hexdigest()[:12]


def _sha256_12(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:12]


# ── 1. 算法与截断 ─────────────────────────────────────────────────

def test_hash_entry_is_sha256_truncated_to_12():
    for text in ("§ 用户喜欢喝热拿铁", "deploy port 8767", " x "):
        assert mem0_sync.hash_entry(text) == _sha256_12(text)
        assert len(mem0_sync.hash_entry(text)) == 12


def test_hash_entry_still_strips_whitespace():
    """换算法不许动既有语义：首尾空白不参与哈希（旧行为逐字保留）。"""
    assert mem0_sync.hash_entry("  abc  ") == mem0_sync.hash_entry("abc")


def test_hash_entry_no_longer_md5():
    """同一输入的 md5 键与 sha256 键必然不同 —— 这条红着才算真换了算法。"""
    assert mem0_sync.hash_entry("user prefers tea") != _md5_12("user prefers tea")


# ── 2. parse_entries 与新键一致（集合相等判据）─────────────────────

def test_parse_entries_keys_match_new_hash():
    content = "§ 条目甲：今天部署了 v20\n§ 条目乙：端口固定在 8767\n§xx"  # 末段 ≤5 字符被弃
    entries = mem0_sync.parse_entries(content)
    texts = [t for _, t in entries]
    assert {h for h, _ in entries} == {_sha256_12(t) for t in texts}
    assert len(entries) == 2


# ── 3. 存量状态迁移（纯函数级）─────────────────────────────────────

def test_migrate_legacy_state_translates_md5_keys():
    """md5 键按当前内容重算平移，memory_id 原值带走；孤儿键丢弃。"""
    keep, drop = "条目甲内容", "条目乙内容"
    legacy = {
        _md5_12(keep): "mem-001",
        _md5_12("已删除的条目"): "mem-ghost",   # 当前 MEMORY.md 里已无此条目
        _sha256_12(drop): "",                    # 已是新格式的键（混存过渡期）
    }
    migrated = mem0_sync.migrate_legacy_state_hashes(legacy, [keep, drop])
    assert migrated == {_sha256_12(keep): "mem-001", _sha256_12(drop): ""}


def test_migrate_is_idempotent_on_new_format_state():
    """新格式状态再过一遍迁移必须逐字节不变 —— 无条件每跑一次才安全。"""
    state = {_sha256_12("条目甲内容"): "mem-001"}
    assert mem0_sync.migrate_legacy_state_hashes(state, ["条目甲内容"]) == state


# ── 4. 端到端：存量 md5 状态升级后**不重推** ───────────────────────

def _wire_tmp_paths(monkeypatch, tmp_path, content: str, state: dict):
    memory_md = tmp_path / "MEMORY.md"
    memory_md.write_text(content, encoding="utf-8")
    sync_state = tmp_path / ".sync_state.json"
    sync_state.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(mem0_sync, "MEMORY_MD", memory_md)
    monkeypatch.setattr(mem0_sync, "SYNC_STATE", sync_state)
    return sync_state


def test_sync_once_with_legacy_md5_state_skips_everything(monkeypatch, tmp_path):
    """★兼容判据：存量 .sync_state.json（md5 键）升级后首次同步零重推，
    且状态文件被平移成 sha256 键。"""
    content = "§ 条目甲：今天部署了 v20\n§ 条目乙：端口固定在 8767"
    legacy_state = {_md5_12(t): "" for _, t in mem0_sync.parse_entries(content)}
    sync_state = _wire_tmp_paths(monkeypatch, tmp_path, content, legacy_state)

    pushed = []

    def fake_push(text, category, source):  # 与 push_to_aidumem 逐参数对齐
        pushed.append((text, category, source))
        return True

    monkeypatch.setattr(mem0_sync, "push_to_aidumem", fake_push)
    result = mem0_sync.sync_once()

    assert pushed == [], f"存量 md5 状态没被平移，{len(pushed)} 条老条目被重推"
    assert result == {"skipped": 2, "new": 0, "errors": 0}
    on_disk = json.loads(sync_state.read_text(encoding="utf-8"))
    assert set(on_disk) == {_sha256_12(t) for _, t in mem0_sync.parse_entries(content)}


def test_sync_once_with_empty_state_pushes_all(monkeypatch, tmp_path):
    """对照组：空状态下条目**真的会被推** —— 防「永远不推」式假绿灯。"""
    content = "§ 条目甲：今天部署了 v20\n§ 条目乙：端口固定在 8767"
    _wire_tmp_paths(monkeypatch, tmp_path, content, {})

    pushed = []

    def fake_push(text, category, source):  # 与 push_to_aidumem 逐参数对齐
        pushed.append((text, category, source))
        return True

    monkeypatch.setattr(mem0_sync, "push_to_aidumem", fake_push)
    result = mem0_sync.sync_once()

    assert {p[1:] for p in pushed} == {("hermes_memory", "mem0_sync")}
    assert len(pushed) == 2 and result["new"] == 2

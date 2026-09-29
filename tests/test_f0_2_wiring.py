"""f0.2 接线整改守卫（外部用户 Windows 自检反馈）。

三处真缺陷，每处结构层（AST/文本）+ 行为层双验，均带负向对照：
  WP1 读线 prefetch 的 /search 必须带 session_id（M2 回声抑制的输入）
  WP2 acceptance_check 的 cron 数期望来自实际 TASKS，不是硬编码常量
  WP3 插件 on_session_end 必须触发萃取（/session/distill → /add）
"""
from __future__ import annotations

import ast
import os
import re

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN = os.path.join(_REPO, "integrations", "hermes-plugin", "aidumem", "__init__.py")
_ACCEPT = os.path.join(_REPO, "scripts", "acceptance_check.sh")
_CRONTAB = os.path.join(_REPO, "scripts", "update_crontab.sh")


def _method_node(src_path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} 不在 {src_path} 里 —— 守卫失去着力点")


# ── WP1：读线带 session ────────────────────────────────────────────

def _search_body_keys_in(func: ast.FunctionDef) -> set:
    """找 func 里对 '/search' 的 try_request 调用，返回其 body dict 的键集合。"""
    for n in ast.walk(func):
        if not isinstance(n, ast.Call):
            continue
        # try_request("POST", "/search", body={...})
        has_search = any(isinstance(a, ast.Constant) and a.value == "/search" for a in n.args)
        if not has_search:
            continue
        for kw in n.keywords:
            if kw.arg == "body" and isinstance(kw.value, ast.Dict):
                return {k.value for k in kw.value.keys if isinstance(k, ast.Constant)}
    return set()


def test_prefetch_search_carries_session_id():
    """读线 prefetch 的 /search 调用必须带 session_id。

    服务端 _req_session_id 顶层就等它；缺了 M2 回声抑制在热路径静默失效、
    ingest_conv_reads_24h 恒 0。用 AST 看真实调用的 body 键，不是 grep 文本。
    """
    keys = _search_body_keys_in(_method_node(_PLUGIN, "prefetch"))
    assert keys, "prefetch 里找不到对 /search 的 body 调用 —— 守卫失去着力点"
    assert "session_id" in keys, (
        f"prefetch 的 /search body 没带 session_id（现有键：{sorted(keys)}）—— "
        "读线不传 session，M2 回声抑制失效"
    )


def test_prefetch_and_sync_turn_symmetric_on_session():
    """负向对照/对称性：写线 sync_turn 带 session_id，读线也必须带。

    这条专防「只修一半电路」重演 —— v21.2 就是写线带了读线漏了。
    """
    read_keys = _search_body_keys_in(_method_node(_PLUGIN, "prefetch"))
    # 写线 sync_turn 的 /add body 也应含 session_id（既有行为，作对照基准）
    sync = _method_node(_PLUGIN, "sync_turn")
    write_has = False
    for n in ast.walk(sync):
        if isinstance(n, ast.Call):
            for kw in n.keywords:
                if kw.arg == "body" and isinstance(kw.value, ast.Dict):
                    for k, v in zip(kw.value.keys, kw.value.values):
                        if isinstance(v, ast.Dict):  # metadata dict
                            if any(isinstance(mk, ast.Constant) and mk.value == "session_id"
                                   for mk in v.keys):
                                write_has = True
    assert write_has, "写线 sync_turn 没带 session_id —— 对照基准失效，请核对"
    assert "session_id" in read_keys, "写线带了 session 读线没带 —— 又只修了半条电路"


# ── WP2：验收脚本不硬编码 cron 数 ──────────────────────────────────

def _acceptance_cron_block() -> str:
    src = open(_ACCEPT, encoding="utf-8").read()
    m = re.search(r"^# >>> gate-cron-installed\n(.*?)^# <<< gate-cron-installed\n",
                  src, re.S | re.M)
    assert m, "acceptance_check 里找不到 cron 实装对账块 —— 守卫失去着力点"
    return m.group(1)


def test_acceptance_cron_count_not_hardcoded():
    """acceptance_check 的 cron 数期望不许是硬编码常量，也不许是恒等式。

    f0.3（B6）：f0.2 把硬编码 `-eq 8` 换成了「TASKS 声明数 vs 同一数组的
    --list」—— 同一份数据数两遍，永远相等。现在必须对账真实 crontab。
    行为验证见 tests/test_f0_3_acceptance_gate.py。
    """
    block = _acceptance_cron_block()
    assert not re.search(r"-eq\s+\d+\b", block), "cron 数仍硬编码为常量"
    assert "update_crontab.sh --installed" in block, "没有对账真实 crontab（--installed）"
    assert "SKIP" in block, "没有 crontab 时必须明确打印 SKIP，而不是算作 PASS"


def test_task_name_pattern_covers_digits():
    """TASKS 计数正则当年漏了 e2e_smoke（[a-z_]+ 不含数字）。

    f0.3：这条自比对已整体移除（它本身是恒等式）；守卫改为钉死它不回来 ——
    验收脚本不许再用 grep 数 update_crontab.sh 的 TASKS 声明来冒充实装数。
    """
    src = open(_ACCEPT, encoding="utf-8").read()
    offenders = [ln.strip()[:120] for ln in src.splitlines()
                 if "grep -c" in ln and "update_crontab" in ln]
    assert not offenders, f"验收脚本又在数 TASKS 声明（恒等式）：{offenders}"


# ── WP3：插件 on_session_end 触发萃取 ──────────────────────────────

def test_on_session_end_triggers_distill():
    """插件 on_session_end 必须触发萃取：/session/distill 且随后 /add 落库。

    射程注意（f0.1+ 同款坑）：只看 on_session_end 函数体里有没有 "/session/distill"
    字符串**不够** —— 萃取逻辑写在嵌套的 _distill 里，即便 _distill 从没被 _spawn，
    字符串也还在函数体内。必须验 _distill **真的被 _spawn 调用了**。
    """
    func = _method_node(_PLUGIN, "on_session_end")

    # ① 找到承载萃取的嵌套函数名（体内含 /session/distill 与 /add 的那个）
    distill_fn = None
    for n in ast.walk(func):
        if isinstance(n, ast.FunctionDef):
            body_strs = [c.value for c in ast.walk(n)
                         if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            if any("/session/distill" in x for x in body_strs) and "/add" in body_strs:
                distill_fn = n.name
    assert distill_fn, (
        "on_session_end 里没有一个嵌套函数同时调 /session/distill 与 /add —— "
        "走推荐插件路径的用户 distill_made 恒 0，拿不到「精华萃取」；"
        "或提炼了却没 /add 落库（精华不进向量库=召回不到=等于没做）"
    )

    # ② 这个嵌套函数必须真的被 _spawn 调用 —— 否则定义在也白搭
    spawned = set()
    for n in ast.walk(func):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "_spawn" and n.args:
            a0 = n.args[0]
            if isinstance(a0, ast.Name):
                spawned.add(a0.id)
    assert distill_fn in spawned, (
        f"萃取函数 {distill_fn} 定义了却没被 _spawn 调用 —— 定义在也不会跑，"
        "这正是 f0.1+ 踩过的『判据扫到定义而非调用』的白护栏"
    )


# ── f0.2 合入生产改动时发现的缺陷：schema 建表不许 once 缓存 ──

def test_ensure_memory_banks_schema_has_no_module_level_once_cache():
    """建表函数不许用模块级 once 缓存短路 —— 键覆盖不了「不同库」这个输入维度。

    生产侧曾给 ensure_memory_banks_schema 加 _MEMORY_BANKS_SCHEMA_INITIALIZED
    全局布尔缓存：生产单库长驻时命中是收益，但传入指向新库的 conn 时照样短路、
    新库不建表 → 下一次查询 no such table。全量测试每个用例各用临时库，第一个
    用例置真后其余 189 个全炸。记忆「缓存失效条件压过收益」的同款坑，故去除。
    本守卫钉死它不复活。
    """
    import ast as _ast

    src_path = os.path.join(_REPO, "ducky", "bank_contract.py")
    tree = _ast.parse(open(src_path, encoding="utf-8").read())
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, _ast.FunctionDef) and n.name == "ensure_memory_banks_schema"), None)
    assert fn is not None, "ensure_memory_banks_schema 不在了 —— 守卫失去着力点"

    # 函数体内不许出现「global <名> + 基于该名的早退 return」这种 once 缓存形态
    globals_declared = set()
    for n in _ast.walk(fn):
        if isinstance(n, _ast.Global):
            globals_declared.update(n.names)
    once_like = {g for g in globals_declared if "INITIALIZED" in g.upper() or "ONCE" in g.upper()
                 or "_DONE" in g.upper() or "CACHED" in g.upper()}
    assert not once_like, (
        f"ensure_memory_banks_schema 又出现 once 缓存全局：{once_like} —— "
        "以全局布尔为键覆盖不了『不同 conn/库』，多库场景会 no such table"
    )

    # 负向对照：确认这个 AST 判据真能抓到 once 缓存形态（构造一个假函数验）
    bad = _ast.parse(
        "def f():\n    global _X_INITIALIZED\n    if _X_INITIALIZED:\n        return\n"
    )
    bad_fn = bad.body[0]
    bad_globals = {nm for n in _ast.walk(bad_fn) if isinstance(n, _ast.Global) for nm in n.names}
    assert any("INITIALIZED" in g.upper() for g in bad_globals), "判据对 once 缓存形态失去识别力"

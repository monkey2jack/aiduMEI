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

def test_acceptance_cron_count_not_hardcoded():
    """acceptance_check 的 cron 数期望不许是硬编码常量。"""
    src = open(_ACCEPT, encoding="utf-8").read()
    # 找那条 crontab intent 的 check 行
    line = next((ln for ln in src.splitlines()
                 if "crontab intent list" in ln and "TASKS" in ln), None)
    assert line, "acceptance_check 里找不到 crontab intent 那条 check —— 守卫失去着力点"
    # 负向对照式判据：不许再出现 `-eq 8` / `-eq 9` 这类拿死数字比的形态
    assert not re.search(r"-eq\s+\d+\b", line), (
        f"cron 数仍硬编码为常量：{line.strip()[:120]} —— "
        "加/删任务时会重演脱节，应改为读实际 TASKS 声明数"
    )
    # 正向：必须两来源自比对（声明数 vs --list）
    assert "grep -c" in line and "--list" in line, (
        "cron 数期望没有从实际 TASKS 声明动态取 —— 应 grep 声明数并与 --list 比对"
    )


def test_task_name_pattern_covers_digits():
    """TASKS 声明的计数正则必须覆盖含数字的任务名（如 e2e_smoke）。

    这本身就是本次踩的坑：[a-z_]+ 漏了 e2e_smoke，数出 8 而非 9。
    """
    src = open(_ACCEPT, encoding="utf-8").read()
    line = next((ln for ln in src.splitlines() if "grep -c" in ln and "update_crontab" in ln), "")
    assert "a-z0-9_" in line or "[[:alnum:]" in line, (
        f"TASKS 计数正则不含数字类，会漏 e2e_smoke 这类名字：{line.strip()[:120]}"
    )


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

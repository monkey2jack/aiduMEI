"""f0.1 · verbatim 事件时间根因回归守卫。

钉死 2026-09-22 LoCoMo 试跑暴露的 P0：原文库把「事情发生的时间」
写成了「记录存进来的时间」，且不报错不告警（假绿灯）。

三条线各有守卫，且每条都配负向对照——没有负向对照的守卫，
在实现退化时可能仍然全绿（见 feedback_guard_failure_modes）。
"""
from __future__ import annotations

import os
import subprocess

from ducky.verbatim_vault import _normalize_ts

EVENT_TS = "1:56 pm on 8 May, 2023"

def test_f01_event_time_beats_now():
    """写线①：metadata 给了事件时间时，绝不能回落 now()。"""
    got = _normalize_ts(None, fallback=EVENT_TS)
    assert got == EVENT_TS, f"事件时间被丢弃，退回了 {got!r}"
    # 负向对照：不给 fallback 时确实会回落 now()，证明上面那条不是永真
    assert _normalize_ts(None) != EVENT_TS

def test_f01_per_turn_timestamp_wins():
    """写线②：逐条 message.timestamp 比批次级 metadata 更精确，优先级更高。"""
    per_turn = "2023-05-08T13:56:00"
    assert _normalize_ts(per_turn, fallback=EVENT_TS) == per_turn

def test_f01_falls_back_to_now_only_when_nothing_given():
    """写线③：两者都缺才用 now()——兜底仍在，没有被改没。"""
    got = _normalize_ts(None, fallback=None)
    assert got and got != EVENT_TS
    assert got.startswith("20")

def test_f01_build_context_reads_top_level_recorded_at():
    """读线：verbatim 条目没有 metadata，时间戳在**顶层** recorded_at。

    漏读它，时序题的证据就会「无时间戳」进上下文——2026-09-22 实测 39%。
    """
    from benchmarks.locomo_official import build_context

    verbatim_item = {  # 逐字复刻 verbatim_search 的返回形状
        "memory": "我昨天去了互助小组。",
        "recorded_at": EVENT_TS,
        "_verbatim": True,
    }
    ctx = build_context([verbatim_item])
    assert ctx.startswith(f"{EVENT_TS}: "), f"顶层时间戳被漏读: {ctx!r}"

    # 负向对照：真没有时间戳时不硬造（官方口径「不硬造」）
    assert build_context([{"memory": "无时间戳的一句话。"}]) == "无时间戳的一句话。"

def test_f01_time_decay_survives_non_iso_event_time():
    """防回归：事件时间可为任意格式，时间衰减不许因此静默归零。

    recorded_at 改后承载调用方原样的事件时间（LoCoMo 是
    "1:56 pm on 8 May, 2023" 这种非 ISO 串），fromisoformat 解析必失败。
    created_at 恒 ISO 且在 extract_timestamp 的 key 顺序中更靠前，作兜底。
    """
    from ducky.scoring import extract_timestamp

    with_backup = {
        "recorded_at": EVENT_TS,             # 非 ISO 事件时间
        "created_at": "2026-09-23 00:23:33",  # ISO 入库时间
        "_verbatim": True,
    }
    assert extract_timestamp(with_backup) > 0, "补了 created_at 仍取不到时间，兜底失效"

    # 负向对照：不补 created_at 时确实归零——证明这条兜底不是白护栏
    assert extract_timestamp({"recorded_at": EVENT_TS, "_verbatim": True}) == 0.0


def test_f01_inject_renders_time_with_memory():
    """生产读线末端：召回注入给模型时必须带上时间。

    生产侧时序问题的真根因不在存储也不在检索——库里存着
    （facts 有 created_at、verbatim 有 recorded_at），/search 也照常返回，
    但 integrations/aidumem-inject.sh 渲染时只取 `memory` 正文，
    时间在注入那一刻被丢掉，模型一问「上次是什么时候」只能猜。
    与评测侧 build_context 漏读时间戳是同一根因的两处发作。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sh = open(os.path.join(repo, "integrations", "aidumem-inject.sh"),
              encoding="utf-8").read()

    marker = "import re\nresults = result.get('results') or []"
    assert marker in sh, "注入渲染段锚点不在了——守卫失去着力点，请同步改判据"
    body = sh[sh.index(marker):sh.index('"\n}', sh.index(marker))]

    harness = (
        "import os, re\n"
        "os.environ['AIDUMEM_SEARCH_LIMIT'] = '3'\n"
        "result = {'results': ["
        "{'memory': 'A事', 'created_at': '2026-09-20T10:11:12+00:00'},"
        "{'memory': 'B事', 'recorded_at': '1:56 pm on 8 May, 2023'},"
        "{'memory': 'C事'}]}\n"
    ) + body

    r = subprocess.run(["python3", "-c", harness], capture_output=True, text=True)
    assert r.returncode == 0, f"注入渲染段跑不起来（shell 内嵌 py 语法错会静默失效）：{r.stderr[:300]}"
    out = r.stdout

    assert "· [2026-09-20] A事" in out, f"ISO 时间未渲染进注入块：{out!r}"
    assert "· [1:56 pm on 8 May, 2023] B事" in out, f"非 ISO 事件时间未渲染：{out!r}"
    # 负向对照：真没有时间就不硬造（与 build_context 同口径）
    assert "· C事" in out and "[] C事" not in out, f"无时间条目被硬造了时间：{out!r}"


# ── f0.1 补充守卫（依据用户审计的三处追问）──────────────────────────

def _inject_render(env_extra: dict) -> str:
    """把注入脚本里那段内嵌 python 抽出来真跑一遍，返回它打印的召回块。

    守卫必须打在**脚本里那段真实代码**上：复制一份到测试里改着玩，
    改的是复制品，脚本坏了守卫照样绿。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sh = open(os.path.join(repo, "integrations", "aidumem-inject.sh"),
              encoding="utf-8").read()
    marker = "import re\nresults = result.get('results') or []"
    assert marker in sh, "注入渲染段锚点不在了——守卫失去着力点，请同步改判据"
    body = sh[sh.index(marker):sh.index('"\n}', sh.index(marker))]

    env_lines = "".join(
        "os.environ[%r] = %r\n" % (k, v) for k, v in env_extra.items()
    )
    harness = (
        "import os, re\n"
        "os.environ['AIDUMEM_SEARCH_LIMIT'] = '3'\n"
        + env_lines +
        "result = {'results': ["
        "{'memory': 'A事', 'created_at': '2026-09-20T10:11:12+00:00'},"
        "{'memory': 'B事', 'recorded_at': '2026-09-20T22:45:00+00:00'},"
        "{'memory': 'C事'}]}\n"
    ) + body
    r = subprocess.run(["python3", "-c", harness], capture_output=True, text=True)
    assert r.returncode == 0, f"注入渲染段跑不起来：{r.stderr[:300]}"
    return r.stdout


def test_f01_inject_date_mode_off_drops_time():
    """AIDUMEI_INJECT_DATE=off 时不带时间——使用者嫌挤可以关掉。"""
    out = _inject_render({"AIDUMEI_INJECT_DATE": "off"})
    assert "· A事" in out, f"off 模式下正文应照常渲染：{out!r}"
    assert "[2026-09-20]" not in out, f"off 模式仍带了日期：{out!r}"
    # 负向对照：默认模式下同样的数据必须带日期，否则本用例没有区分力
    assert "[2026-09-20]" in _inject_render({}), "默认模式没带日期——off 的断言失去意义"


def test_f01_inject_date_mode_minute_adds_time_of_day():
    """AIDUMEI_INJECT_DATE=minute 时精确到分——用于区分同一天内的先后。"""
    out = _inject_render({"AIDUMEI_INJECT_DATE": "minute"})
    assert "· [2026-09-20 10:11] A事" in out, f"minute 模式未渲染到分：{out!r}"
    assert "· [2026-09-20 22:45] B事" in out, f"minute 模式未渲染到分：{out!r}"
    # 负向对照：day 模式必须只到天，否则说明粒度开关根本没生效
    day_out = _inject_render({"AIDUMEI_INJECT_DATE": "day"})
    assert "10:11" not in day_out, f"day 模式漏出了时分——粒度开关未生效：{day_out!r}"


def test_f01_inject_date_mode_invalid_falls_back_to_day():
    """写错值按默认 day 走，不因为一个拼写错误把时间整段丢掉。"""
    out = _inject_render({"AIDUMEI_INJECT_DATE": "DaY_typo"})
    assert "· [2026-09-20] A事" in out, f"非法值未回落 day：{out!r}"


def test_f01_timestamp_key_priority_created_at_beats_recorded_at():
    """钉死 TIMESTAMP_KEY_PRIORITY 里 created_at 必须排在 recorded_at 之前。

    f0.1 起 recorded_at 承载调用方任意格式的事件时间；若它被提到前面，
    时间衰减会在解析失败时静默归零，**不报错、不告警**。
    """
    from ducky.scoring import TIMESTAMP_KEY_PRIORITY, extract_timestamp

    keys = list(TIMESTAMP_KEY_PRIORITY)
    assert "created_at" in keys and "recorded_at" in keys
    assert keys.index("created_at") < keys.index("recorded_at"), (
        "created_at 必须排在 recorded_at 之前，否则不可解析的事件时间会让时间衰减静默归零"
    )

    # 行为侧：两者都在时取 created_at（而不是非 ISO 的 recorded_at）
    item = {"created_at": "2026-09-20T10:11:12+00:00",
            "recorded_at": "1:56 pm on 8 May, 2023"}
    assert extract_timestamp(item) > 0, "created_at 在场却没被用上——优先级失效"

    # 负向对照：只有非 ISO 的 recorded_at 时确实归零（证明这个顺序在救场，不是摆设）
    assert extract_timestamp({"recorded_at": "1:56 pm on 8 May, 2023"}) == 0.0, (
        "负向对照失效：非 ISO recorded_at 本应解析失败归零"
    )


def test_f01_hook_deployment_checker_detects_drift():
    """部署一致性自检必须真能判出漂移，且零钩子不报绿。"""
    import importlib.util
    import tempfile

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "_chk", os.path.join(repo, "scripts", "check_hook_deployment.py"))
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)

    with tempfile.TemporaryDirectory() as td:
        host = os.path.join(td, "inject.sh")
        cfg = os.path.join(td, "config.yaml")
        repo_src = os.path.join(repo, "integrations", "aidumem-inject.sh")
        open(cfg, "w").write(
            "hooks:\n  pre_llm_call:\n    - command: \"%s\"\n      timeout: 8\n" % host)

        # ① 宿主是旧版 → 必须判 drift（这正是用户审计实锤的那个场景）
        open(host, "w").write("#!/bin/sh\necho old\n")
        rep = chk.check(cfg, repo)
        assert rep["checked"] == 1, f"没解析到钩子路径，守卫射程为 0：{rep}"
        assert not rep["ok"] and rep["items"][0]["status"] == "drift", f"旧版未判漂移：{rep}"

        # ② 部署到位 → 必须判 ok（负向对照：证明 ① 不是恒红）
        import shutil
        shutil.copyfile(repo_src, host)
        rep2 = chk.check(cfg, repo)
        assert rep2["ok"] and rep2["items"][0]["status"] == "ok", f"一致却未判通过：{rep2}"

        # ③ 宿主声明的文件不存在 → 判 missing，不许当成通过
        os.remove(host)
        rep3 = chk.check(cfg, repo)
        assert not rep3["ok"] and rep3["items"][0]["status"] == "missing", f"缺文件未判红：{rep3}"

    # ④ 空配置不报绿（「没测到」≠「通过」）
    rep4 = chk.check(os.devnull, repo)
    assert rep4["no_hooks_found"] and not rep4["ok"], f"零钩子被判成通过：{rep4}"


def test_f01_hook_checker_catches_renamed_stale_hook():
    """宿主用了**别名**且内容是旧的——必须判 🔴 drift，不许降级成「无法判定」。

    这正是 f0.1 用户审计实锤的真实场景：宿主 config 指向 mem0-inject.sh，
    仓库里只有 aidumem-inject.sh。按文件名认源会找不到而放过，
    所以本脚本按**事件**认源。
    """
    import importlib.util
    import shutil
    import tempfile

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "_chk2", os.path.join(repo, "scripts", "check_hook_deployment.py"))
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)

    with tempfile.TemporaryDirectory() as td:
        # 别名：叫 mem0-inject.sh，仓库里没有这个名字
        host = os.path.join(td, "mem0-inject.sh")
        cfg = os.path.join(td, "config.yaml")
        open(cfg, "w").write(
            "hooks:\n  pre_llm_call:\n    - command: \"%s\"\n      timeout: 8\n" % host)

        open(host, "w").write("#!/bin/sh\n# 旧版，没有日期渲染\necho '{}'\n")
        rep = chk.check(cfg, repo)
        assert rep["items"][0]["status"] == "drift", (
            "改名 + 旧内容被降级了，最该报警的场景反而放过：%s" % rep)
        assert rep["items"][0]["repo_path"].endswith("aidumem-inject.sh"), (
            "没按事件认回真源：%s" % rep)

        # 负向对照：别名但内容是新的 → 必须判 ok（证明上面判的是内容不是名字）
        shutil.copyfile(os.path.join(repo, "integrations", "aidumem-inject.sh"), host)
        rep2 = chk.check(cfg, repo)
        assert rep2["ok"], "别名但内容一致却被判红——判的是名字不是内容：%s" % rep2

"""tests/test_v20_reasoning_effort_switch.py — v20 P1-4：`reasoning_effort` 不许再是假开关

用户视角审计 A3（也是我自己上一轮自首的一条）：`reasoning_effort="none"` 这个开关是死的，
日志照报 ✅。

实测把它的三张脸都翻出来了（计划书里只点了一处，实际有三处）：

1. `ducky/speed/config.py` 的默认值就是 `"none"` —— 于是**每个部署**都在无声地往
   每一次补全请求里塞 `reasoning_effort=none`；
2. `ducky/speed/patch.py` 的注入代码是
   `if force_effort and "reasoning_effort" not in kwargs: … elif force_effort: …`
   —— 两个分支做的是同一件事，一段谁都没读懂的死分支；紧接着
   `logger.info("✅ speed LLM patch: … effort=none")` 无条件报成功；
3. `ducky/routes_config.py` 把这个字段兜底成 `"none"` 再吐给控制台 ——
   **配置里一个字都没写，界面上却显示旋钮已设好**。

而 `ducky/llm_client.py` 的 🔴-B 注释（v19.4.0 生产实测）早就写明：
**上游网关无视请求级 `reasoning_effort`/`enable_thinking`。**

所以「设了没用但报成功」这第三态，是三个文件合起来搭出来的。

整改口径不是删功能：开源用户可能把 base_url 指向别的供应商（OpenAI o 系列认这个
字段）。口径是 **保留能力、去掉默认、日志只报确实做了的事**。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_reasoning_effort_switch.py -v
"""
from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ducky.speed.config as speed_config  # noqa: E402
import ducky.speed.patch as speed_patch  # noqa: E402

_ROOT = pathlib.Path(_REPO_ROOT)


class _Completions:
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, *a, **kw):
        self.calls.append(dict(kw))
        return "ok"


class _Chat:
    def __init__(self):
        self.completions = _Completions()


class _Client:
    def __init__(self):
        self.chat = _Chat()


class _Llm:
    def __init__(self):
        self.client = _Client()


class _Mem:
    def __init__(self):
        self.llm = _Llm()


@pytest.fixture
def _speed_cfg(monkeypatch, tmp_path):
    """让 load_speed_cfg 读一份临时配置，并绕开 mtime 缓存。"""
    cfg_path = tmp_path / "mem0_config_local.json"

    def _write(speed: dict, llm_max_tokens: int = 2048):
        cfg_path.write_text(json.dumps({
            "_speed": speed,
            "llm": {"config": {"max_tokens": llm_max_tokens}},
        }), encoding="utf-8")
        monkeypatch.setattr(speed_config, "_CFG_PATH", str(cfg_path))
        monkeypatch.setattr(speed_patch, "_CFG_PATH", str(cfg_path))
        monkeypatch.setattr(speed_config, "_speed_cfg_cache", None)
        monkeypatch.setattr(speed_config, "_speed_cfg_mtime", 0.0)
    return _write


# ═══════════════ ① 没配 → 一个字都不发、一个字都不提 ═══════════════

def test_unset_effort_is_not_injected_at_all(_speed_cfg, caplog):
    """★ 核心断言：部署方没配 `force_reasoning_effort` 时，请求里**不该出现**它。

    整改前这里必然出现 `reasoning_effort="none"`，因为默认值就是 "none"。
    """
    _speed_cfg({})  # 什么都不配，走默认
    mem = _Mem()
    with caplog.at_level(logging.INFO, logger="aiduMEM.speed"):
        speed_patch.patch_llm_for_speed(mem)

    mem.llm.client.chat.completions.create(model="m", messages=[])
    call = mem.llm.client.chat.completions.calls[-1]

    assert "reasoning_effort" not in call, (
        f"没配却往请求里塞了 reasoning_effort={call.get('reasoning_effort')!r} —— "
        "上游网关实测无视该字段，塞进去纯属自欺"
    )
    assert call.get("max_tokens") == 2048, "max_tokens 那半边（真生效的那半边）不该受影响"

    msgs = " / ".join(r.getMessage() for r in caplog.records)
    assert "reasoning_effort" not in msgs or "未配置" in msgs, (
        f"没做的事不许出现在成功日志里：{msgs}"
    )


def test_default_config_no_longer_ships_a_dead_effort_value():
    """★ 默认值本身：`_DEFAULT_SPEED` 不许再带一个「塞了也不生效」的值。

    这是整条缺陷的源头 —— 出厂默认就带着坑，每个部署都继承。
    """
    default = speed_config._DEFAULT_SPEED.get("force_reasoning_effort", "__missing__")
    assert default in (None, "", "__missing__"), (
        f"出厂默认仍是 {default!r} —— 每个部署都会无声地注入一个不生效的字段。"
        "保留这个键是为了给别家供应商留能力，但默认必须是「不设」"
    )


# ═══════════════ ② 显式配了 → 照发，但日志只敢说「已发送」 ═══════════════

def test_explicit_effort_is_injected_and_logged_as_sent_not_as_effective(_speed_cfg, caplog):
    """★ 保留能力：显式配了就照发（别家供应商认这个字段）。

    但日志的措辞是判据的一部分：只许说「已按配置发送」，不许说「已生效」——
    请求侧压根判定不了上游采不采纳。这条断言把措辞焊住。
    """
    _speed_cfg({"force_reasoning_effort": "low"})
    mem = _Mem()
    with caplog.at_level(logging.INFO, logger="aiduMEM.speed"):
        speed_patch.patch_llm_for_speed(mem)

    mem.llm.client.chat.completions.create(model="m", messages=[])
    call = mem.llm.client.chat.completions.calls[-1]
    assert call.get("reasoning_effort") == "low", (
        "显式配置没被注入 —— 整改把能力一起删掉了，别家供应商的用户被连坐"
    )

    msgs = [r.getMessage() for r in caplog.records]
    hit = [m for m in msgs if "reasoning_effort" in m]
    assert hit, f"发了却没说，运维无从知道请求里多了个字段：{msgs}"
    said = hit[0]
    assert "已按配置发送" in said, f"日志措辞没写明「只是发送」：{said}"
    for overclaim in ("已生效", "生效成功", "已启用"):
        assert overclaim not in said, (
            f"日志声称了请求侧无法判定的结果（「{overclaim}」）：{said}"
        )


def test_caller_supplied_effort_is_still_overridden_by_explicit_config(_speed_cfg):
    """配置显式设了值时，调用方自带的值以配置为准 —— 保持整改前的语义。

    整改只动「默认值」和「日志」，不许顺手改掉「显式配置优先」这条既有语义：
    那属于夹带，不属于 P1-4。
    """
    _speed_cfg({"force_reasoning_effort": "high"})
    mem = _Mem()
    speed_patch.patch_llm_for_speed(mem)
    mem.llm.client.chat.completions.create(model="m", messages=[], reasoning_effort="low")
    assert mem.llm.client.chat.completions.calls[-1]["reasoning_effort"] == "high"


# ═══════════════ ③ 控制台视图不许凭空造一个值 ═══════════════

def test_config_view_reports_absent_effort_as_absent(monkeypatch):
    """★ 「配置没写却显示写了」也是假绿灯。

    `routes_config._build_config_view()` 原先把这个字段兜底成 `"none"`，于是控制台
    上那个旋钮看着像已经设好了，而配置文件里一个字都没有。
    """
    import ducky.routes_config as rc

    monkeypatch.setattr(rc, "_load_raw_config", lambda: {
        "llm": {"provider": "openai", "config": {"model": "m"}},
    })
    view = rc._build_config_view()
    got = view["llm"]["config"]["reasoning_effort"]
    assert got is None, (
        f"配置里没有 reasoning_effort，视图却报 {got!r} —— 控制台会把它显示成"
        "「已配置」，运维照着这个显示做判断就会错"
    )

    # 正向对照：真配了就要如实报出来，别一刀切成 None
    monkeypatch.setattr(rc, "_load_raw_config", lambda: {
        "llm": {"provider": "openai", "config": {"model": "m", "reasoning_effort": "low"}},
    })
    assert rc._build_config_view()["llm"]["config"]["reasoning_effort"] == "low", (
        "真配了却报不出来 —— 判据从一个极端跑到另一个极端"
    )


# ═══════════════ ④ 射程守卫：死分支与硬编码兜底不许回魂 ═══════════════

def test_no_module_hardcodes_a_reasoning_effort_fallback():
    """★ 元测试：语法树里不许再出现「兜底成一个 effort 字符串」的形状。

    判据是 AST 级的：本文件和被改的三个文件里都正当地写着 `"none"` 这个词
    （在解释它为什么被删掉的注释里）。字符串级判据分不清「代码在兜底」和
    「注释在说不再兜底」。

    形状定义：`X.get("reasoning_effort" | "force_reasoning_effort", <非空字符串>)`。
    """
    KEYS = {"reasoning_effort", "force_reasoning_effort"}
    offenders = []
    for path in sorted((_ROOT / "ducky").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and len(node.args) == 2):
                continue
            key, default = node.args
            if not (isinstance(key, ast.Constant) and key.value in KEYS):
                continue
            if isinstance(default, ast.Constant) and isinstance(default.value, str) \
                    and default.value != "":
                offenders.append(
                    f"{path.relative_to(_ROOT).as_posix()}:{node.lineno} "
                    f"→ get({key.value!r}, {default.value!r})")
    assert not offenders, (
        "以下位置把 reasoning_effort 兜底成了一个字符串常量：\n  "
        + "\n  ".join(offenders)
        + "\n上游网关实测无视这个字段（见 ducky/llm_client.py 的 🔴-B 注释）。"
          "兜一个值出来只会让「没配」看起来像「配好了」。"
          "保留能力靠的是「显式配了才发」，不是靠默认值。"
    )


def test_the_injection_branch_is_not_a_dead_if_elif():
    """★ 死分支不许回魂：注入 effort 的地方只许有一条判断。

    原代码是 `if force_effort and "reasoning_effort" not in kwargs: A
    elif force_effort: A` —— 两个分支体一模一样。这种「看着有讲究、其实是同一件事」
    的代码，是「谁都没读懂所以谁都不敢动」的典型来源。

    判据：`patch.py` 的 `_wrapped` 里，以 `reasoning_effort` 为赋值目标的语句
    只许出现一次。
    """
    src = (_ROOT / "ducky/speed/patch.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    assigns = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                        and t.slice.value == "reasoning_effort":
                    assigns.append(node.lineno)
    assert len(assigns) == 1, (
        f"kwargs['reasoning_effort'] 的赋值出现了 {len(assigns)} 次（行 {assigns}）—— "
        "多于一次就是死分支回来了：两个分支做同一件事，读者以为其中有讲究"
    )

"""
v20.0 守卫③：``scope_predicate(include_legacy_aliases=True)`` 的调用点管控。

**这条守卫冻结的是一个潜伏参数的爆炸半径，不是一次已发生的事故。**

``ducky/bank_contract.py`` 的 ``scope_predicate()`` 带一个 ``include_legacy_aliases``
参数。默认 ``False`` 时归属判据是「只认 ``user_id``」；传 ``True`` 时放宽成
「``user_id`` OR ``source`` OR ``agent_id``」。老库里 ``source``/``agent_id`` 是
自由文本列，一旦参与归属匹配，租户隔离就等于开了一道后门。

危险全在于它读起来完全无害：参数名像「兼容老数据」，一个赶迁移的人很容易顺手
传 ``True``，**而且传完测试还是绿的** —— 本地库里根本没有跨域的老行可撞，要撞
得上生产那种混着历史写法的库。等它在生产上被发现，已经读出去了。

**射程内当前实测 0 处命中，这是有意的：**本仓目前没有任何一个 ``True`` 调用点。
这条守卫因此是**预置**而不是清扫 —— 它要在「第一个 ``True`` 调用点被写下来」的
那一刻变红，而不是等它上线以后靠事故来发现。写清楚这一点，是为了让下一个人
不要因为「它从来没红过」而以为它没用。

**为什么是守卫而不是删参数：**删掉参数会让真正需要看见老行的迁移脚本没法写，
到时候人只会在别处手抄一份放宽版 WHERE，那比留着更糟 —— 手抄的那份没人守。
所以留参数、留出口，但让每一次使用都必须先在这里登记。

双保险（缺一条都会退化）：
  · 运行时：``scope_predicate`` 内 ``warnings.warn(RuntimeWarning)`` + ``logger.warning``
  · 静态：本文件扫全仓调用点，不在允许名单里当场变红
运行时那条会被 ``-W ignore`` 静音，静态这条不会；静态这条看不见动态构造的调用，
运行时那条看得见。
"""

import ast
import pathlib
import warnings

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_SKIP_PARTS = frozenset({
    "__pycache__", ".venv", "venv", "node_modules", ".git",
    "build", "dist", ".eggs", ".pytest_cache", "backups", ".upgrade-artifacts",
})

# 射程地板。与 test_v20_import_shadowing.py 同一条理由：谁往 _SKIP_PARTS 多塞
# 一项、或把 rglob 换成某个子目录，守卫照样全绿而什么都没查。少查看不见。
_FILE_COUNT_FLOOR = 180

# 被守的参数名。写成常量是为了让「参数改名了但守卫没跟」当场看得出来 ——
# 见 test_guarded_parameter_still_exists。
_GUARDED_KW = "include_legacy_aliases"

# ── 允许名单 ───────────────────────────────────────────────────────────
# 每一项：(相对路径, 理由)。理由必须写清「为什么这里非放宽不可」以及「什么时候
# 可以删掉」，不许写 TODO、不许留空 —— 见 test_allowlist_does_not_rot。
# 当前为空：本仓没有任何一处需要放宽判据。
_ALLOWED: dict[str, str] = {}


def legacy_alias_sites(source):
    """返回 [(行号, 被调函数名)]，命中「显式传 ``_GUARDED_KW=True``」的调用。

    只认**字面 True**。``=flag`` 这种动态值静态看不出来，交给运行时那条告警；
    这里宁可漏报也不误报，因为误报会逼着后人往名单里塞假条目，名单一脏就废了。
    """
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != _GUARDED_KW:
                continue
            if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", "<?>")
                hits.append((node.lineno, name))
    return sorted(hits)


def _iter_py_files():
    return sorted(
        p for p in _REPO_ROOT.rglob("*.py")
        if not any(
            part in _SKIP_PARTS or part.startswith("venv-") or part.startswith("backup-")
            for part in p.relative_to(_REPO_ROOT).parts
        )
    )


def test_no_unlisted_legacy_alias_callers():
    """全仓：不得出现未登记的 ``include_legacy_aliases=True`` 调用点。"""
    unlisted = []
    for path in _iter_py_files():
        if path.name == pathlib.Path(__file__).name:
            continue  # 本文件的合成用例里有字面量，不算调用点
        try:
            sites = legacy_alias_sites(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for lineno, fn in sites:
            if rel not in _ALLOWED:
                unlisted.append(f"{rel}:{lineno} → {fn}(...)")

    assert not unlisted, (
        "出现未登记的放宽归属判据调用点：\n  " + "\n  ".join(unlisted) +
        f"\n\n{_GUARDED_KW}=True 会把 source/agent_id 拉进归属匹配，"
        "等于给租户隔离开后门。若确属迁移期必需，请在本文件 _ALLOWED 里登记，"
        "并写明理由和退出条件；否则改用默认的 scope_predicate()。")


def test_allowlist_does_not_rot():
    """名单不许烂成永真：条目失效、或理由写空/写 TODO，都变红。

    豁免表一旦没人管就会退化成一句「永真」—— 这是 v20.0 甲3 已经踩过的形状，
    这里照抄同一套反腐烂断言。
    """
    for rel, reason in _ALLOWED.items():
        path = _REPO_ROOT / rel
        assert path.exists(), (
            f"_ALLOWED 里的 {rel} 已不存在 —— 请删掉这条，别让名单留着空头条目")
        sites = legacy_alias_sites(path.read_text(encoding="utf-8"))
        assert sites, (
            f"_ALLOWED 里的 {rel} 已经不再传 {_GUARDED_KW}=True —— "
            "请删掉这条豁免，否则它会一直替将来新写的调用点挡住守卫")
        text = (reason or "").strip()
        assert text and "TODO" not in text.upper(), (
            f"_ALLOWED[{rel}] 的理由写空或写成 TODO —— "
            "豁免必须带得住人看的理由和退出条件")


def test_classifier_bites_and_does_not_false_fire():
    """合成负向对照：判据必须咬得住真形状，且不咬相邻的假形状。

    没有这条，上面那个 0 命中的全绿证明不了任何事 —— 判据写错了也是绿。
    """
    bites = (
        "from ducky.bank_contract import scope_predicate\n"
        "frag, args = scope_predicate(scope, include_legacy_aliases=True)\n"
    )
    assert legacy_alias_sites(bites) == [(2, "scope_predicate")], (
        "判据咬不住「显式传 True」这个真形状")

    # 相邻假形状：显式 False、传变量、同名参数给了别的函数但值是 False、
    # 以及仅仅在字符串/注释里出现这串字。一个都不许咬。
    quiet = (
        "frag = scope_predicate(scope, include_legacy_aliases=False)\n"
        "frag = scope_predicate(scope, include_legacy_aliases=flag)\n"
        "note = 'include_legacy_aliases=True'  # 只是文字\n"
        "# include_legacy_aliases=True\n"
    )
    assert legacy_alias_sites(quiet) == [], (
        f"判据误咬了非调用形状：{legacy_alias_sites(quiet)}")


def test_runtime_guard_actually_warns():
    """活体：真调一次放宽分支，必须当场 ``RuntimeWarning``；默认分支必须安静。

    静态守卫看不见动态构造的调用（``scope_predicate(**kw)``），运行时这条能。
    两条都在，才算把出口守住。
    """
    from ducky.bank_contract import make_scope, scope_predicate

    scope = make_scope()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        frag, args = scope_predicate(scope, include_legacy_aliases=True)
    msgs = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    assert msgs, "放宽判据时没有发出 RuntimeWarning —— 运行时那道留痕没了"
    assert _GUARDED_KW in msgs[0], f"告警文案没点名参数，看的人不知道该改哪里：{msgs[0]}"
    # 顺手把「放宽」这件事本身钉死：它确实多认了 source/agent_id。
    assert "source" in frag and "agent_id" in frag, (
        "放宽分支的 SQL 里没有 source/agent_id —— 要么实现变了，要么这条测试测错了分支")

    # 负向对照：默认分支不许 warn，否则告警会被淹成背景噪音，等于没有。
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        frag2, _ = scope_predicate(scope)
    assert not [w for w in caught if issubclass(w.category, RuntimeWarning)], (
        "默认分支也在告警 —— 噪音会让真正的放宽调用被忽略")
    assert "source" not in frag2 and "agent_id" not in frag2, (
        "默认分支竟然也认 source/agent_id —— 隔离已经漏了")


def test_guarded_parameter_still_exists():
    """参数改名/删除时，这条守卫必须自曝，而不是继续全绿。

    守卫最常见的死法不是被删，是它守的东西改名了而它自己不知道 ——
    从此永远 0 命中，永远绿。
    """
    import inspect

    from ducky.bank_contract import scope_predicate

    params = inspect.signature(scope_predicate).parameters
    assert _GUARDED_KW in params, (
        f"scope_predicate 已经没有 {_GUARDED_KW} 参数了 —— "
        "如果是有意删除，请连本文件一起删；否则这条守卫从现在起永远是绿的空转")
    assert params[_GUARDED_KW].default is False, (
        f"{_GUARDED_KW} 的默认值不再是 False —— 放宽判据变成了默认行为，这是隔离事故")


def test_guard_reach_is_not_silently_narrowed():
    """射程不许悄悄变小，且射程里确实包含定义点。"""
    files = _iter_py_files()
    assert len(files) >= _FILE_COUNT_FLOOR, (
        f"射程只剩 {len(files)} 个 .py（地板 {_FILE_COUNT_FLOOR}）—— "
        "排除表或遍历起点被收窄了，守卫会继续全绿但什么都没查")

    rels = {p.relative_to(_REPO_ROOT).as_posix() for p in files}
    assert "ducky/bank_contract.py" in rels, "定义点不在射程内 —— 这个位置必须被查"

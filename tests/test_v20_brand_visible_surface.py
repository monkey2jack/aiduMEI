"""v20.0：把 frontend 之外的「用户可见品牌面」也纳入守卫。

背景（真实事件，2026-08-21）：
一台已部署机器上有人做了一次品牌顺手清理，把三处 `aiduMEM` 改成了 `aiduMEI`：

  1. `integrations/aidumem-inject.sh` 注入进对话的 `[aiduMEM Recall]` 前缀 —— 改对了；
  2. `scripts/health_check.py` 打印给运维看的那行 `🧠 aiduMEM 健康检查` —— 改对了；
  3. `ducky/hot/health.py` 的 `service=f"aiduMEM-v…"` —— **改错了**，那是机器契约，
     生产监控按 `aiduMEM-v*` 匹配，改完 `/health` 返回 `aiduMEI-v…`，
     告警从那一刻起安静失配，服务看着一切正常。

第 3 处其实早有守卫（`tests/test_v19_4_2_brand_surface.py::
test_health_service_field_keeps_internal_name`），连报错文案都预判了「它长得像品牌残留，
所以最容易被下一个人顺手改干净」。守卫是对的，只是没起作用 —— 因为改的是**机器上的文件**，
不是仓里的源码，测试压根没跑。门是好的，绕过门的办法是不走门。

而第 1、2 处的问题相反：**两个方向都没人管**。
`test_v19_4_2_brand_surface._user_visible_files()` 的射程是 `frontend/**/*.{html,js,css,json}`
加 `manifest.json`，仅此。它的 docstring 自称「集合本身就是断言的一部分」—— 在 frontend 内
成立，但集合静默排除了 frontend 之外**所有**给人看的字。于是那两处被改成 aiduMEI 没人报警，
v20 把它们改回 aiduMEM 也不会报警。一个值在两个方向上都能无声翻转，等于这个值没有守卫。

这个文件把那两处正向钉住（必须是当前品牌名），并把机器契约那三处一起写在这里 ——
不是为了重复断言，是为了让「哪些改、哪些刻意不改」在同一屏里读得完整，
下一个拿 sed 的人不必先猜。
"""

import os

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ── 用户可见门面（frontend 之外）：必须是当前品牌名 aiduMEI ──────────────
# (相对路径, 必须出现, 必须不出现, 为什么它算「露脸」)
_VISIBLE = [
    (
        "integrations/aidumem-inject.sh",
        "['[aiduMEI Recall]']",
        "[aiduMEM Recall]",
        "这一行原样出现在下一轮对话里，用户直接读到",
    ),
    (
        "scripts/health_check.py",
        "aiduMEI 健康检查",
        "aiduMEM 健康检查",
        "这一行是 print 出来给运维读的，不是注释",
    ),
]

# ── 机器契约：刻意保留历史内部名 aiduMEM（决策 D2，见 v19.4.2）────────────
# 主责守卫在 tests/test_v19_4_2_brand_surface.py，这里同列一份，
# 只为让「改 / 不改」的分界在一处可读完。
_MACHINE = [
    (
        "ducky/hot/health.py",
        'service=f"aiduMEM-v',
        "/health 的 service 字段：生产监控按 aiduMEM-v* 匹配",
    ),
    (
        "api_server.py",
        'logging.getLogger(f"aiduMEM-v',
        "logger 名：生产日志采集按 aiduMEM-* 过滤",
    ),
    (
        "ducky/hot/health.py",
        'logging.getLogger("aiduMEM.hot")',
        "同上，hot 模块的 logger 名",
    ),
]


def test_pinned_files_all_exist_and_table_is_not_empty():
    """先证明这张表还指着真东西。

    文件被改名或搬走时，逐条断言会因为读不到文件而报错；但如果哪天有人把表清空
    （或用循环跳过缺失文件），守卫会变成一条永远通过的空转 —— 空集不算通过。
    """
    assert _VISIBLE, "用户可见面表为空 —— 这条守卫等于没开"
    assert _MACHINE, "机器契约表为空 —— 这条守卫等于没开"
    missing = [
        rel
        for rel in {row[0] for row in _VISIBLE} | {row[0] for row in _MACHINE}
        if not os.path.isfile(os.path.join(_REPO_ROOT, rel))
    ]
    assert not missing, "守卫表指向了不存在的文件（改名/搬家没同步）：" + ", ".join(sorted(missing))


@pytest.mark.parametrize(
    ("rel", "expected", "forbidden", "why"),
    _VISIBLE,
    ids=[row[0] for row in _VISIBLE],
)
def test_user_visible_surface_uses_current_brand(rel, expected, forbidden, why):
    """frontend 之外的露脸文字必须是 aiduMEI。"""
    src = _read(rel)
    assert expected in src, (
        f"{rel} 的用户可见文字不是当前品牌名（{why}）。\n"
        f"  期望包含: {expected}"
    )
    assert forbidden not in src, (
        f"{rel} 又出现了历史品牌名 {forbidden}（{why}）。\n"
        "  若是被一次全局 sed 顺手改回去的：注意同一次 sed 很可能也动了机器契约，"
        "见本文件 _MACHINE 表。"
    )


@pytest.mark.parametrize(
    ("rel", "expected", "why"),
    _MACHINE,
    ids=[f"{row[0]}::{row[1][:24]}" for row in _MACHINE],
)
def test_machine_contract_keeps_internal_name(rel, expected, why):
    """机器契约必须仍是 aiduMEM —— 改它会让监控/采集安静失效。

    2026-08-21 的实测后果：某台部署机上这处被改成 aiduMEI 后，`/health` 返回
    `aiduMEI-v19.5.0`，按 `aiduMEM-v*` 匹配的告警自那一刻起再没匹配上过，
    而服务本身一切正常 —— 失败与成功无法区分。
    """
    src = _read(rel)
    assert expected in src, (
        f"{rel} 的机器契约被改名了（{why}）。\n"
        "  这不是品牌门面。若确实要改，须同步改生产侧采集/告警规则，"
        "并把决策记进 CHANGELOG —— 只改代码等于把告警静音。"
    )

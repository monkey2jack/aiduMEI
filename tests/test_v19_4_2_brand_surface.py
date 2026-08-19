"""
tests/test_v19_4_2_brand_surface.py — 品牌门面守卫（含反向的「别改过头」守卫）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么有这个文件
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v19.4.1 做过一次 aiduMEM → aiduMEI 的改名，改完看着是干净的，
但控制台页脚上仍然明晃晃地印着 `aiduMEM` —— 用户第一眼看到的那一行。

漏掉的原因很具体：字标是**标签拆分**写法

    <span class="lbl">aidu<b>MEM</b></span>

`grep -rn aiduMEM` 扫不到它，`sed -i s/aiduMEM/aiduMEI/g` 也改不到它。
改名当时用的正是全局替换，于是全仓 sed 报告「已全部替换」，
而唯一有人肉眼会看的那处残留了下来 —— 又一个**假绿灯**。

所以这里的断言不查「全仓有没有 aiduMEM」（那样会误伤大量历史内部名，
见下方反向守卫），而是把**用户可见门面**这个集合单独圈出来锁死：
前端 HTML/JS/CSS、manifest.json、FastAPI /docs 封面标题。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
另一半同样重要：反向守卫（决策 D2 的可执行化）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`AIDUMEM_*` 环境变量、logger 名、`/health` 的 `service` 字段是**机器契约**：
生产侧的日志采集与监控按这些字符串匹配。一次「顺手改干净」的全局替换
会让告警规则集体失配，而且失配的表现是**安静地再也不告警**。

因此本文件同时断言这些内部名**保持不变**。它不是「还没改完」的遗留，
是 v19.4.2 明确拍板不动的边界（决策 D2）。谁将来想动，
必须先让这条测试变红、看到这段说明、再决定是不是真要动。
"""

import os
import pathlib
import re
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_ROOT = pathlib.Path(_REPO_ROOT)
_FRONTEND = _ROOT / "frontend"

# 历史内部名。出现在用户可见门面里就是缺陷，出现在机器契约里是刻意保留。
_LEGACY = "aiduMEM"
# 环境变量前缀属机器契约，大小写与品牌名不同，需从残留扫描里排除。
_ENV_PREFIX = re.compile(r"AIDUMEM_[A-Z0-9_]+")
# 备份文件不是发布物
_BACKUP = re.compile(r"\.bak(-|\.|$)|~$")


def _user_visible_files():
    """用户可见门面的**完整集合**。

    集合本身就是断言的一部分：新增前端页面会自动进入扫描范围，
    不需要谁记得来这里补一行。
    """
    files = []
    if _FRONTEND.is_dir():
        for path in sorted(_FRONTEND.rglob("*")):
            if not path.is_file() or path.suffix not in (".html", ".js", ".css", ".json"):
                continue
            if _BACKUP.search(path.name):
                continue
            files.append(path)
    manifest = _ROOT / "manifest.json"
    if manifest.is_file():
        files.append(manifest)
    return files


def _strip_machine_contract(text: str) -> str:
    """抹掉环境变量名等机器契约，只留下「人会读到的字」。"""
    return _ENV_PREFIX.sub("", text)


def test_user_visible_surfaces_carry_no_legacy_brand():
    """用户可见门面里不得再出现 aiduMEM。

    这是 v19.4.1 实际漏出去的那个缺陷的守卫。
    """
    files = _user_visible_files()
    assert files, "用户可见门面集合为空 —— 扫描范围写错了，这条守卫等于没开"

    offenders = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _LEGACY in _strip_machine_contract(line):
                rel = path.relative_to(_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()[:100]}")

    assert not offenders, (
        "用户可见门面残留历史品牌名 aiduMEM：\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("page", ["index.html", "login.html"])
def test_wordmark_is_split_across_tags_and_still_correct(page):
    """字标必须是 aidu<b>MEI</b> —— 并显式记录它扫不到的事实。

    这条断言写死了「标签拆分」这个形态，而不是宽松地查 'MEI' 有没有出现。
    理由：形态本身就是缺陷成因。只要还是拆分写法，全局替换就还会漏，
    这条测试就必须一直站在这里。
    """
    path = _FRONTEND / page
    text = path.read_text(encoding="utf-8")

    assert "aidu<b>MEI</b>" in text, (
        f"{page} 的页脚字标不是 aidu<b>MEI</b>。"
        "注意它是标签拆分写法，grep/sed 都扫不到，必须手工核对这一行。"
    )
    assert "aidu<b>MEM</b>" not in text, f"{page} 页脚字标仍是旧品牌 aidu<b>MEM</b>"


@pytest.mark.parametrize("page", ["index.html", "login.html"])
def test_page_title_is_the_canonical_brand(page):
    """浏览器标签页标题 —— 用户可见门面里最显眼的一处。"""
    text = (_FRONTEND / page).read_text(encoding="utf-8")
    m = re.search(r"<title>(.*?)</title>", text, re.S)
    assert m, f"{page} 缺少 <title>"
    assert m.group(1).strip() == "aiduMEI⚕爱嘟优忆思", (
        f"{page} 标题为 {m.group(1).strip()!r}，与规范品牌形式不符"
    )


def test_manifest_declares_current_brand_and_codename():
    """manifest.json 是分发侧的门面（版本号一致性另有守卫，这里只管品牌）。"""
    import json

    data = json.loads((_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert data["name"] == "aiduMEI", f"manifest name 为 {data['name']!r}"
    assert _LEGACY not in data["description"], "manifest description 残留 aiduMEM"


def test_fastapi_docs_title_uses_current_brand():
    """/docs 封面标题是 API 侧唯一的品牌门面。

    直接读源码而不是起服务：这条守卫要能在没有依赖、没有端口的环境里跑。
    """
    src = (_ROOT / "api_server.py").read_text(encoding="utf-8")
    m = re.search(r'title=f?"(aiduME[IM][^"]*API[^"]*)"', src)
    assert m, "api_server.py 里找不到 FastAPI(title=...) 的品牌标题"
    assert m.group(1).startswith("aiduMEI"), (
        f"/docs 封面标题仍是旧品牌：{m.group(1)!r}"
    )


# ────────────────────────────────────────────────────────────────
# 反向守卫：以下内部名**刻意不改**（决策 D2）
# 生产侧日志采集与监控按它们匹配，改动会让告警安静地失效。
# ────────────────────────────────────────────────────────────────

def test_health_service_field_keeps_internal_name():
    """`/health` 的 `service` 字段是机器契约，v19.4.2 明确不动。

    它长得像品牌残留，所以最容易被下一个人「顺手改干净」。
    这条测试就是拦在那一刻的说明书。
    """
    src = (_ROOT / "ducky" / "hot" / "health.py").read_text(encoding="utf-8")
    assert 'service=f"aiduMEM-v' in src, (
        "/health 的 service 字段被改名了。这是机器契约，不是品牌门面 —— "
        "生产监控按 aiduMEM-v* 匹配，改了会让告警安静失效。"
        "若确实要改，须同步改生产侧采集规则，并把决策记进 CHANGELOG。"
    )


def test_logger_names_keep_internal_name():
    """logger 名同样是机器契约（日志采集按前缀过滤）。"""
    src = (_ROOT / "api_server.py").read_text(encoding="utf-8")
    assert 'logging.getLogger(f"aiduMEM-v' in src, (
        "api_server 的 logger 名被改了 —— 生产日志采集按 aiduMEM-* 过滤"
    )
    health = (_ROOT / "ducky" / "hot" / "health.py").read_text(encoding="utf-8")
    assert 'logging.getLogger("aiduMEM.hot")' in health, (
        "health 模块的 logger 名被改了 —— 同上"
    )


def test_env_var_prefix_is_untouched():
    """`AIDUMEM_*` 是用户 `.env` 里已经写好的键名。

    改前缀 = 所有既有部署的配置在升级后**静默失效**（键不匹配就当没配），
    这正是本版在修的那一类故障。
    """
    utils = (_ROOT / "ducky" / "utils.py").read_text(encoding="utf-8")
    assert "AIDUMEM_API_TOKEN" in utils, "凭据环境变量前缀被改动，会让既有部署静默失配"

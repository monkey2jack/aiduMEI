"""`.env.example` 必须真的是完整清单（参赛前自查 WP-E · 宣称证伪）。

两份 README 都写着「完整清单连注释见 `.env.example`」。这是一句**宣称**，
而宣称即承诺 —— 所以它得可验证。

发现经过：把「代码里真的会去读的键」与「样例里列出的键」做集合差，
差出 **13 个**，其中包含 `AIDUMEM_ALLOW_INSECURE_PUBLIC`（决定要不要
无凭据监听公网）、`AIDUMEM_INJECTION_GUARD_MODE`（注入防御档位）
这类安全相关的开关，以及我前一天刚加的 `AIDUMEI_RECALL_MIN_HYBRID`。
**一个配置项如果只有读它的代码知道它存在，那它对部署方就等于不存在。**
"""

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 允许不出现在样例里的键，**每一条都要写清为什么**。
#: 空豁免表比长豁免表好；要加一条，先问它是不是真的不该让部署方看见。
_EXEMPT = {
    # 测试自用的逃生门，不是部署配置面
    "AIDUMEI_TEST_ALLOW_REAL_DATA_DIR": "测试专用：允许把 DATA_DIR 指向真实目录",
    "AIDUMEI_TEST_BACKUP_HOME": "测试专用：备份门禁的临时 HOME",
}


def _keys_code_actually_reads() -> set:
    """扫「真的从环境里取值」的位点，不扫注释里提到的名字。

    这条区分很重要：本仓的注释里大量引用环境变量名做说明，
    按字面 grep 会把它们全算进来，得出一个虚高的分母
    （v20.2.5 记过「grep 分不清代码和注释」）。
    """
    code = ""
    for p in list((_ROOT / "ducky").rglob("*.py")) + [
        _ROOT / "api_server.py", _ROOT / "mcp_server.py",
    ]:
        code += p.read_text(encoding="utf-8")
    keys = set(re.findall(
        r"(?:environ\.get|getenv|environ\[)\(?\s*[\"']"
        r"(AIDUME[IM]_[A-Z0-9_]+|UI_DIR|MEM0_[A-Z0-9_]+)[\"']", code))
    keys |= set(re.findall(
        r"(?:float_env|int_env|str_env|bool_env)\(\s*[\"']"
        r"(AIDUME[IM]_[A-Z0-9_]+)[\"']", code))
    return keys


def _keys_listed_in_example() -> set:
    ex = (_ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(
        r"^#?\s*(AIDUME[IM]_[A-Z0-9_]+|UI_DIR|MEM0_[A-Z0-9_]+)\s*=", ex, re.M))


def test_env_example_lists_every_key_the_code_reads():
    """代码读的每一个键，样例里都要有一行（注释掉也算，那是样例该有的样子）。"""
    missing = sorted(_keys_code_actually_reads() - _keys_listed_in_example() - set(_EXEMPT))
    assert not missing, (
        f"这些键代码里会读，`.env.example` 却没列：{missing}\n"
        "  两份 README 都写着「完整清单见 .env.example」—— 清单说自己完整就得真的完整。\n"
        "  要么补进样例（推荐），要么加进本文件的 _EXEMPT 并写清为什么部署方不该看见它。"
    )


def test_example_does_not_advertise_keys_that_no_longer_exist():
    """反方向也要红：样例里列着、代码却早就不读了 —— 那是死配置项。

    部署方照着设，行为不变，还以为自己调过了。**两个方向都不会自己报错。**
    """
    code = ""
    for p in list((_ROOT / "ducky").rglob("*.py")) + [
        _ROOT / "api_server.py", _ROOT / "mcp_server.py",
        *(_ROOT / "integrations").rglob("*"),
        *(_ROOT / "scripts").rglob("*"),
    ]:
        if p.is_file():
            try:
                code += p.read_text(encoding="utf-8")
            except Exception:
                pass
    # 有些键是**按前缀动态拼出来**的（`core_memory.py` 的
    # `_STALENESS_ENV_PREFIX + 块名`）。按字面找不到，但它们确实会被读 ——
    # 判据必须认得这种形态，否则守卫会把「读得到的」误报成「死配置」，
    # 而下一个人学到的是「见红就往豁免表里加一条」，守卫从此失效。
    prefixes = [ln.split('"')[1] for ln in code.splitlines()
                if "_ENV_PREFIX = " in ln and '"' in ln]
    def _reachable(k: str) -> bool:
        return (k in code) or any(k.startswith(px) and len(k) > len(px) for px in prefixes)

    stale = sorted(k for k in _keys_listed_in_example() if not _reachable(k))
    assert not stale, (
        f"样例里列着但全仓再没人读的键：{stale} —— 部署方照着设会以为自己调过了"
    )


def test_exemptions_all_carry_a_reason():
    """豁免表里每一条都必须有理由 —— 没有理由的豁免会长期堆积成一张白名单。"""
    for k, why in _EXEMPT.items():
        assert why and len(why) > 6, f"{k} 的豁免没写清理由"


def test_both_readmes_document_the_same_configuration():
    """中英两份 README 的环境变量表必须**逐键相等**。

    发现经过：中文表有 5 个键英文表没有，其中包括 `AIDUMEM_API_TOKEN` ——
    **决定这个服务对外要不要鉴权的那一个**。只读英文文档的部署者，
    从头到尾不会知道有这个开关。英文表另有 3 个键中文表没有。

    一个双语项目，两份文档对「怎么配这个系统」给出不同答案，
    比只有一份文档更糟：读者不知道自己读的是不是全的。
    """
    def table_keys(name):
        t = (_ROOT / name).read_text(encoding="utf-8")
        return set(re.findall(r"\|\s*`(AIDUME[IM]_[A-Z0-9_]+|UI_DIR)`\s*\|", t))

    zh, en = table_keys("README.md"), table_keys("README_EN.md")
    assert zh == en, (
        f"两份 README 的变量表不一致 —— 只在中文：{sorted(zh - en)}；"
        f"只在英文：{sorted(en - zh)}。读者不知道自己读的那份是不是全的。"
    )
    assert "AIDUMEM_API_TOKEN" in zh, (
        "鉴权开关从变量表里消失了 —— 那是决定这个服务对外要不要设防的那一个，"
        "任何一份文档都不许漏"
    )

def test_readme_claims_match_runtime_facts():
    """v20.3 WP-A：README 的关键事实不许靠记忆写。

    VOC 核实出 12 条文档与代码不一致。这类漂移的共同根因是：文档数字
    由人手维护。MCP 端口、工具数、Python 版本先纳入机械对表。
    """
    import ast

    root = pathlib.Path(__file__).resolve().parent.parent
    mcp = (root / "mcp_server.py").read_text(encoding="utf-8")
    tree = ast.parse(mcp)
    default_port = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            if node.args and getattr(node.args[0], "value", None) == "--port":
                for kw in node.keywords:
                    if kw.arg == "default":
                        default_port = ast.literal_eval(kw.value)
    assert default_port == 8766

    tools = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "attr", None) == "tool":
                    tools.append(node.name)
    assert len(tools) == 41
    for name in ("README.md", "README_EN.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert ":8768" not in text, f"{name} 仍写着错误 MCP 端口"
        assert "3.12+" not in text, f"{name} 仍写着 3.12+；pyproject 是 >=3.10"
        assert "41 tools" in text or "41 工具" in text
        if name == "README.md":
            assert "服务自身不做鉴权" not in text

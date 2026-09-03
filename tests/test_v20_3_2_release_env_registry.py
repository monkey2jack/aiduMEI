"""v20.3.2 正式版 · P1-15：环境变量名单一真相源（用户审计 H / GLM F-3）。

双前缀 90+ 个变量、分界无规律、拼错静默失效 —— 作者本人同一版本踩了两次。
守卫三件事：① 注册表与源码 AST 抽取**完全相等**（新增忘登记 / 登记了不存在的名都红）；
② 文档（.env.example）里出现的名字必须都是真的；③ 启动期真的会对未知名出声并给近似名。
"""
import ast
import logging
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_registry_equals_what_the_source_actually_reads():
    """**P1-15 靶心**：KNOWN_ENV_VARS 必须等于源码真正出现的名字集合。"""
    from ducky import env_registry as er
    actual = er.extract_env_names_from_source(_ROOT)
    only_in_registry = sorted(er.KNOWN_ENV_VARS - actual)
    only_in_source = sorted(actual - er.KNOWN_ENV_VARS)
    assert not only_in_source, f"源码新读了这些变量但注册表没登记：{only_in_source}"
    assert not only_in_registry, f"注册表登记了源码已不读的名字：{only_in_registry}"
    assert er.extract_dynamic_prefixes_from_source(_ROOT) == er.DYNAMIC_ENV_PREFIXES, (
        "动态前缀表与源码不一致（以 _ 结尾的前缀常量）")


def test_the_famous_typo_is_not_a_known_name():
    """AIDUMEI_DATA_DIR 是踩过两次的**错名**，只该出现在叙事里，不许进注册表。"""
    from ducky.env_registry import KNOWN_ENV_VARS
    assert "AIDUMEI_DATA_DIR" not in KNOWN_ENV_VARS
    assert "AIDUMEM_DATA_DIR" in KNOWN_ENV_VARS


def test_env_example_only_documents_real_names():
    """文档里出现的每个 AIDUME?_ 名字都必须是代码真读的。"""
    from ducky.env_registry import is_known_env_name
    text = (_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"AIDUME[IM]_[A-Z0-9_]+", text))
    fake = sorted(n for n in documented if not is_known_env_name(n))
    assert not fake, f".env.example 写了代码不读的变量名（用户照抄必静默失效）：{fake}"


def test_unknown_names_are_detected_with_a_close_match():
    from ducky.env_registry import unknown_env_vars
    found = unknown_env_vars({"AIDUMEI_DATA_DIR": "/x", "AIDUMEM_API_TOKEN": "t", "HOME": "/h"})
    assert set(found) == {"AIDUMEI_DATA_DIR"}, found
    assert found["AIDUMEI_DATA_DIR"] and found["AIDUMEI_DATA_DIR"][0] == "AIDUMEM_DATA_DIR", (
        f"最近似名不是 AIDUMEM_DATA_DIR：{found}")


def test_startup_actually_warns(caplog):
    from ducky.env_registry import warn_unknown_env_vars
    with caplog.at_level(logging.WARNING, logger="aiduMEM.env_registry"):
        warn_unknown_env_vars({"AIDUMEI_LOG_DIR": "/l"})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("AIDUMEI_LOG_DIR" in m and "AIDUMEM_LOG_DIR" in m for m in msgs), msgs


def test_lifespan_wires_the_warning():
    """定义了不接线 = 没做。lifespan 必须调用它。"""
    tree = ast.parse((_ROOT / "api_server.py").read_text(encoding="utf-8"))
    ls = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_lifespan")
    calls = {getattr(c.func, "id", "") for c in ast.walk(ls) if isinstance(c, ast.Call)}
    assert "warn_unknown_env_vars" in calls, "lifespan 没调 warn_unknown_env_vars"

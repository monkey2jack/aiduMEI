"""v20.3.2 正式版 · 边界与契约：P1-7 MCP bank_id / P1-11 top_k / P1-13 配置读取 / P1-14 main 顺序。

四条外审发现的共同形态：**「limit 有上限」却被另一个无界参数覆盖；「多域隔离」在
最重要的 Agent 生态里根本传不进去；「原子写」保证的是写过程不出半个文件，不保证
写的内容基于正确的旧状态；「拒绝启动」发生在建表起线程之后。** 代码都对，边界不对。
"""
import ast
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ══════════════════════════════════════════════════════════════
# P1-7 · MCP 工具必须能传 bank_id（Gemini P0-3：9 个工具 0 个带）
# ══════════════════════════════════════════════════════════════

def _mcp_tools():
    tree = ast.parse((_ROOT / "mcp_server.py").read_text(encoding="utf-8"))
    out = []
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef):
            continue
        if not any(isinstance(d, ast.Call) and getattr(getattr(d.func, "value", None), "id", "") == "mcp"
                   and getattr(d.func, "attr", "") == "tool" for d in n.decorator_list):
            continue
        params = [a.arg for a in n.args.args]
        out.append((n.name, params, n))
    return out


def test_every_memory_tool_that_takes_user_id_also_takes_bank_id():
    """**P1-7 靶心**：凡有 user_id 的工具都必须有 bank_id —— 否则 Agent 全被锁死在 default 域。

    v20 最核心的跃迁是 (user_id, bank_id) 三元组隔离；REST 契约有它，MCP 契约没有。
    Claude Desktop / Cursor / Hermes 全走 MCP —— 花大代价做的多域隔离在那儿不可达。
    """
    tools = _mcp_tools()
    assert tools, "没扫到 @mcp.tool —— 守卫失去着力点"
    missing = [name for name, params, _ in tools if "user_id" in params and "bank_id" not in params]
    assert not missing, f"这些 MCP 工具收 user_id 却不收 bank_id，调用方永远落在 default 域：{missing}"


def test_bank_id_is_actually_forwarded_not_just_accepted():
    """签名有 bank_id 不等于传给了 API —— 每个收 bank_id 的工具，函数体里必须把它放进请求。"""
    problems = []
    for name, params, node in _mcp_tools():
        if "bank_id" not in params:
            continue
        src = ast.get_source_segment((_ROOT / "mcp_server.py").read_text(encoding="utf-8"), node) or ""
        body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
        if '"bank_id"' not in body and "'bank_id'" not in body and "bank_id=" not in body:
            problems.append(name)
    assert not problems, f"收了 bank_id 却没往下传（「定义了不接线」）：{problems}"


def test_facts_tools_take_bank_id_too():
    """facts_* 工具同样要带域：事实库也是按 (user_id, bank_id) 隔离的。"""
    names = {name: params for name, params, _ in _mcp_tools()}
    for t in ("facts_search", "facts_add"):
        assert t in names, f"找不到 {t}"
        assert "bank_id" in names[t], f"{t} 不收 bank_id"


# ══════════════════════════════════════════════════════════════
# P1-11 · top_k 必须有上限（Codex F-03）
# ══════════════════════════════════════════════════════════════

def test_top_k_over_100_is_rejected_at_the_model():
    """**P1-11 靶心**：limit 有 1–100 的边界，top_k 却是裸 int 且**覆盖** limit。"""
    from pydantic import ValidationError

    from ducky.api_models import SearchRequest
    with pytest.raises(ValidationError):
        SearchRequest(query="q", top_k=100_000)
    with pytest.raises(ValidationError):
        SearchRequest(query="q", top_k=-1)


def test_top_k_zero_and_100_are_still_accepted():
    """**回归**：0 = 「用 limit」；100 = 上限本身，都合法。MCP 默认传 top_k=5。"""
    from ducky.api_models import SearchRequest
    assert SearchRequest(query="q", top_k=0).top_k == 0
    assert SearchRequest(query="q", top_k=100).top_k == 100
    assert SearchRequest(query="q", top_k=5).top_k == 5


def test_backend_candidate_fanout_is_capped():
    """降级路径把 effective_limit×3 交给后端 —— 乘积也要有硬顶，不许靠输入边界间接保证。"""
    src = (_ROOT / "ducky" / "hot" / "search.py").read_text(encoding="utf-8")
    assert "effective_limit * 3" not in src or "min(" in src, (
        "search.py 仍把 effective_limit*3 裸传后端；请套 min(..., 硬顶)")


# ══════════════════════════════════════════════════════════════
# P1-13 · 配置读取失败不许当空配置覆盖（Codex F-05）
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    import ducky.routes_config as rc
    cfg = tmp_path / "mem0_config_local.json"
    monkeypatch.setattr(rc, "_CFG_PATH", str(cfg), raising=True)
    return rc, cfg


def test_missing_config_reads_as_empty(cfg_env):
    """文件**不存在**是唯一允许初始化为空配置的情形。"""
    rc, cfg = cfg_env
    assert not cfg.exists()
    assert rc._load_raw_config_for_write() == {}


def test_corrupted_config_refuses_to_be_read_as_empty(cfg_env):
    """**P1-13 靶心**：JSON 损坏 / 不可读 ≠ 空配置。

    原实现任何异常一律 `return {}`，随后 PUT 基于这个空字典写临时文件并 replace ——
    原子写只保证不出半个文件，**不保证写的是对的旧状态**。一次瞬时 I/O 抖动 +
    一次保存，其他全部配置段就没了。
    """
    rc, cfg = cfg_env
    cfg.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(rc.ConfigUnreadable):
        rc._load_raw_config_for_write()


def test_put_on_corrupted_config_leaves_the_file_byte_identical(cfg_env):
    """端到端：配置损坏时 PUT 必须拒绝，且原文件**一个字节都不变**。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    rc, cfg = cfg_env
    garbage = b"{ broken json \xe4\xb8\xad"
    cfg.write_bytes(garbage)
    app = FastAPI()
    rc.register_config_routes(app)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.put("/config/llm", json={"config": {"model": "x"}})
    assert r.status_code in (409, 500), f"损坏配置上 PUT 回了 {r.status_code}，应拒绝"
    assert cfg.read_bytes() == garbage, "PUT 在损坏配置上覆盖了原文件 —— 数据丢失"


def test_put_on_missing_config_still_creates_it(cfg_env):
    """**回归**：首次配置（文件不存在）照旧能建。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    rc, cfg = cfg_env
    app = FastAPI()
    rc.register_config_routes(app)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.put("/config/llm", json={"provider": "openai", "config": {"model": "x", "api_key": "k"}})
    assert r.status_code == 200, r.text
    assert json.loads(cfg.read_text(encoding="utf-8"))["llm"]["config"]["model"] == "x"


# ══════════════════════════════════════════════════════════════
# P1-14 · main() 不许在安全检查前产生副作用（Codex F-09）
# ══════════════════════════════════════════════════════════════

def test_main_does_not_start_background_itself():
    """lifespan 是后台生命周期唯一负责人；main() 里直接起后台 = 两套真相源。"""
    tree = ast.parse((_ROOT / "api_server.py").read_text(encoding="utf-8"))
    main_fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [getattr(c.func, "id", "") for c in ast.walk(main_fn) if isinstance(c, ast.Call)]
    assert "_start_background" not in calls, (
        "main() 直接调了 _start_background —— 一个最终会被安全策略拒绝启动的进程，"
        "退出前已经建表、预热、起线程")
    assert "_enforce_public_binding_policy" in calls and "_enforce_single_process_policy" in calls


def test_lifespan_validates_before_it_starts_anything():
    """lifespan 内：两条策略检查的源码位置必须都在 _start_background 之前。"""
    src = (_ROOT / "api_server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ls = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_lifespan")
    seq = [getattr(c.func, "id", "") for c in ast.walk(ls) if isinstance(c, ast.Call)]
    seq = [s for s in seq if s in ("_enforce_public_binding_policy", "_enforce_single_process_policy", "_start_background")]
    assert seq.index("_start_background") == len(seq) - 1, f"lifespan 调用序 {seq}：后台启动必须在最后"

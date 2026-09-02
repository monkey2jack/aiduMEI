"""v20.3.2 · 零凭据首跑全路由扫描（第 10 轮外部审计 P1-5）

**为什么要有这个文件。** 前九轮审计全部跑在凭据齐全的机器上 —— 生产机配好了
mem0，`get_memory()` 永远成功，于是「后端未配置」这条分支**从来没被任何一次
审计走过**。而它恰恰是新用户的第一站：README 主动推荐「不装可选依赖、不配密钥
也能跑」，`/add/raw` 的 503 指引里也写着「暂时不想配可以先用 /add/raw」。

本轮把整份公开仓克隆到一个干净 venv、什么都不配、起真实 app 打全部路由，
当场抓到 3 个 500。500 的意思是「服务端自己炸了」—— 而它唯一的成因是
「用户还没配置」。**把「还没配」报成「我坏了」，是首跑体验里最贵的一种谎。**

这个文件就是那台「什么都不配的机器」。它必须：
  · 在修复前红（见文件末尾的区分力说明）；
  · 每次跑都重新扫全部路由，新增路由自动进入射程 —— 不靠有人记得来加用例。
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re

import pytest
from fastapi import HTTPException


# ── 夹具：一个「什么都没配」的世界 ──────────────────────────────────────────
#
# 关键在**怎么**造这个未配置状态。两种做法只有一种是对的：
#   ❌ mock 掉 get_memory 让它抛异常 —— 那测的是「后端坏了」，判据应当算失败；
#   ✅ 让配置文件真的不存在 —— 这才是新用户的第一站，判据应当算未启用。
# 用 tmp_path 把配置路径指到一个不存在的文件，其余全部走真实代码。
# ── 为什么用子进程，而不是 TestClient ──────────────────────────────────────
#
# 第一版用 `importlib.reload(api_server)` 在**本进程内**造未配置世界。
# 单跑全绿，全量跑就红 —— 前序测试早已把 SQLite 线程连接、mem0 单例、
# DATA_DIR 解析结果缓存进模块态，reload 只重建了 app，没重建这些。
# 于是扫出来的 500 是**测试互相踩出来的**，不是产品的。
#
# 而「新用户的第一次」本义就是一个**干净进程**：什么都没配、什么都没缓存。
# 用子进程跑，既贴合语义，又天然免疫本进程的一切污染 ——
# 判据要落在真实世界上，不能落在「我这一轮测试恰好留下的状态」上。
_SWEEP_SNIPPET = r'''
import json, os, re, sys
sys.path.insert(0, sys.argv[1])
# 全部环境由父进程显式给（见 child_env）；这里只补两个非路径开关。
os.environ["AIDUMEI_ENGINE_MODE"] = "auto"
os.environ.setdefault("AIDUMEI_RECALL_VERDICT_THRESHOLD", "0")
os.environ.setdefault("AIDUMEI_RECALL_MIN_HYBRID", "0")
import logging
logging.disable(logging.CRITICAL)
from fastapi.testclient import TestClient
from api_server import app
c = TestClient(app, raise_server_exceptions=False)
SCOPE = {"user_id": "first_run", "bank_id": "default"}
GENERIC = {**SCOPE, "query": "网关端口是多少", "content": "网关端口是 22012",
           "messages": "网关端口是 22012", "memory_id": "probe-id",
           "confirm": True, "dry_run": True}
SKIP_PREFIXES = ("/docs", "/openapi", "/redoc", "/ui", "/static", "/favicon")

# Rev.2：路由表枚举不等于业务逻辑被执行。高风险面必须给**合法请求形状**，
# 其它路由若被 Pydantic 422 拒绝，单独记 validation_rejected，不准计入 exercised。
# request spec 的键是 (method, route-template)，值只使用 TestClient.request 接受的参数。
SPECS = {
    ("POST", "/add"): {"json": {**SCOPE, "messages": "网关端口是 22012", "infer": False}},
    ("POST", "/add/raw"): {"json": {**SCOPE, "content": "网关端口是 22012", "metadata": {}}},
    ("POST", "/search"): {"json": {**SCOPE, "query": "网关端口是多少", "limit": 3}},
    ("POST", "/search_trace"): {"json": {**SCOPE, "query": "网关端口是多少", "limit": 3}},
    ("POST", "/delete"): {"json": {**SCOPE, "memory_id": "probe-id"}},
    ("DELETE", "/delete"): {"params": {**SCOPE, "memory_id": "probe-id"}},
    ("POST", "/delete_all"): {"json": {**SCOPE, "confirm": True}},
    ("POST", "/graduate"): {"params": {**SCOPE, "dry_run": True}},
    ("POST", "/reload"): {},
    ("POST", "/persona/build"): {"json": {"persona_card": "测试人格", "persona_name": "probe", "persona_key": "probe", "use_llm": False}},
    ("GET", "/persona/banks"): {"params": {"persona_key": "probe"}},
    ("GET", "/persona/detail"): {"params": {"bank_id": 1}},
    ("POST", "/persona/retrieve"): {"json": {"situation": "测试情境", "persona_key": "probe", "bank_id": 1, "k": 1}},
    ("POST", "/persona/rollback"): {"json": {"persona_key": "probe", "to_version": 1}},
    ("GET", "/persona/context"): {"params": {"persona_key": "probe", "situation": "测试", "k": 1}},
    ("GET", "/persona/ai-self"): {"params": SCOPE},
    ("POST", "/persona/ai-self/add"): {"params": {"category": "general", "key": "probe", "value": "probe", "bank_id": "default"}},
    ("GET", "/facts"): {"params": SCOPE},
    ("POST", "/facts/add"): {"params": {**SCOPE, "category": "general", "fact_key": "probe", "fact_value": "probe"}},
    ("GET", "/facts/search"): {"params": {**SCOPE, "query": "probe", "top_k": 3}},
    ("GET", "/facts/entities"): {"params": {**SCOPE, "entity": "probe"}},
    ("GET", "/facts/related"): {"params": {**SCOPE, "entity": "probe"}},
    ("GET", "/facts/reason"): {"params": {**SCOPE, "entities": "probe_a,probe_b"}},
    ("GET", "/facts/entities/list"): {"params": {**SCOPE, "limit": 3}},
    ("POST", "/facts/feedback"): {"params": {**SCOPE, "fact_id": 1, "helpful": True}},
    ("POST", "/facts/tags/generate"): {"params": {"fact_id": 1}},
    ("POST", "/facts/preference"): {"params": {**SCOPE, "fact_id": 1, "score": 0.5}},
    ("POST", "/facts/expire"): {"params": {**SCOPE, "fact_id": 1, "expires_in_hours": 24}},
    ("GET", "/facts/delta"): {"params": {**SCOPE, "since": "2026-01-01T00:00:00Z"}},
    ("POST", "/facts/compress"): {"data": {"text": "probe\nerror probe"}},
    ("POST", "/facts/inject-context"): {"json": {**SCOPE, "query": "probe", "k": 1}},
    ("GET", "/code/graph"): {"params": {}},
    ("POST", "/code/impact"): {"json": {"changed_files": ["ducky/mem0_runtime.py"]}},
}
HIGH_RISK = set(SPECS)

def materialize(path):
    # 常见 int 参数用 1，其余路径参数用 probe-id；保留模板用于 SPECS 查表。
    return re.sub(r"\{(?:bank_id|fact_id|edit_id|refine_id|candidate_id|tombstone_id)\}", "1",
                  re.sub(r"\{[^}]+\}", "probe-id", path))

rows = []
seen = set()
counts = {"exercised": 0, "validation_rejected": 0, "skipped": 0}
for r in getattr(app, "routes", []):
    path = getattr(r, "path", "")
    for method in sorted(getattr(r, "methods", []) or []):
        key = (method, path)
        if method in ("HEAD", "OPTIONS") or key in seen:
            continue
        seen.add(key)
        if not path or path.startswith(SKIP_PREFIXES) or method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            rows.append([method, path, "SKIP", "not_an_api_probe", "skipped"])
            counts["skipped"] += 1
            continue
        url = materialize(path)
        if "{" in url:
            rows.append([method, path, "SKIP", "unresolved_path_parameter", "skipped"])
            counts["skipped"] += 1
            continue
        spec = dict(SPECS.get(key, {}))
        if key not in SPECS:
            if method == "GET":
                spec["params"] = SCOPE
            elif method == "DELETE":
                spec["params"] = {**SCOPE, "memory_id": "probe-id"}
            else:
                spec["json"] = GENERIC
        try:
            resp = c.request(method, url, **spec)
            verdict = "validation_rejected" if resp.status_code == 422 else "exercised"
            counts[verdict] += 1
            rows.append([method, path, resp.status_code, resp.text[:200], verdict])
        except Exception as exc:
            counts["exercised"] += 1
            rows.append([method, path, "EXC", f"{type(exc).__name__}: {exc}"[:200], "exercised"])

# 自报世界状态：BASE_DIR / DATA_DIR / 是否鉴权 / 后端配置判定。
# 判据不能只数状态码 —— 它必须先确认自己测的是以为在测的那个世界。
from ducky.utils import BASE_DIR, DATA_DIR
from ducky.mem0_runtime import mem0_backend_configured
print(json.dumps({
    "routes": len(seen), "hits": len(rows), "rows": rows, "counts": counts,
    "high_risk": [[m, p] for m, p in sorted(HIGH_RISK)],
    "world": {
        "base_dir": BASE_DIR, "data_dir": DATA_DIR,
        "auth_enabled": bool(os.environ.get("AIDUMEM_API_TOKEN", "").strip()),
        "backend": list(mem0_backend_configured()),
    },
}))
'''


@pytest.fixture(scope="module")
def sweep_result(tmp_path_factory):
    """在**干净子进程**里枚举全部路由，并返回逐条执行分类及世界状态。"""
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    d = tmp_path_factory.mktemp("zerocred")
    script = d / "sweep.py"
    script.write_text(_SWEEP_SNIPPET, encoding="utf-8")
    # **只给一个最小环境**，不继承父进程 env。
    #
    # 第一版子进程继承了 os.environ，全量跑时前序测试泄漏的
    # `AIDUMEM_API_TOKEN` 跟着进来，扫描于是拿到一片 401 —— 那是**鉴权在正常工作**，
    # 不是缺陷，但我的判据（首跑无凭据）就此失真。
    # 「新用户的第一次」本义就是一个干净环境：没有 token、没有配置、没有缓存。
    # 显式白名单传参，既贴合语义，又对**任何**未来可能泄漏的变量免疫 ——
    # 逐个去清已知变量名，等于假设我们已知道全部会漏什么。
    # 路径类变量必须用 **AIDUMEM_** 前缀 —— 那是 utils.py:97 真正读的名字
    # （`AIDUMEI_` 是新变量的约定前缀，DATA_DIR 属冻结兼容面，不吃它）。
    # 上一版这里写的是 AIDUMEI_DATA_DIR，被静默忽略，于是扫描子进程
    # **把测试库写进了仓库自己的 data/** —— 是 `test_v20_subprocess_env_isolation.py`
    # 那条守卫（手搓 env 必须钉 AIDUMEM_DATA_DIR）把它抓出来的。
    # HOME 也不继承父进程：给一个 tmp 目录，杜绝任何往真实家目录写的可能。
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(d / "home"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AIDUMEM_DATA_DIR": str(d / "data"),
        "AIDUMEM_LOG_DIR": str(d / "log"),
        # **前缀必须是 AIDUMEM_（冻结兼容面），不是 AIDUMEI_。**
        # 上一版这里写的是 AIDUMEI_HOME —— utils.py:94 读的是 AIDUMEM_HOME，
        # 于是 BASE_DIR 仍指向真实仓库根，启动时 `load_env_file()` 把部署树里的
        # `.env` 读了进来（含 API token），扫描拿到一片 401。
        # 这是**同一个坑在本文件里第二次踩**（第一次是 AIDUMEI_DATA_DIR），
        # 所以底下 test_bare_environment_is_really_bare 加了「未鉴权」这条断言：
        # 新变量的约定前缀是 AIDUMEI_，而 HOME/DATA_DIR/LOG_DIR/CONFIG_FILE
        # 属冻结兼容面，读的是 AIDUMEM_ —— 写错前缀不会报错，只会静默失效。
        "AIDUMEM_HOME": str(d / "home"),
        "AIDUMEM_CONFIG_FILE": str(d / "no_such_config.json"),
        # 显式给空：load_env_file 不覆盖已存在的键，所以这一句同时挡住
        # 「.env 里配了 token」与「父进程泄漏了 token」两条路。
        "AIDUMEM_API_TOKEN": "",
        # **光给空 token 不够**：`env_or_env_file()` 在环境变量为空时会
        # **回读 .env 文件**（`utils.py:188-194`）—— 部署树上那份真 .env 于是
        # 照样把 token 递进去，扫描拿到 401。生产机实测就是这么露出来的：
        # 本机没有 .env，这个回退路径从来没被走到过。
        # 用产品自己支持的开关把 .env 指开：`_env_file_path()` 认 AIDUMEM_ENV_FILE。
        "AIDUMEM_ENV_FILE": str(d / "no_such_env"),
    }
    proc = subprocess.run(
        [sys.executable, str(script), str(root)],
        capture_output=True, text=True, timeout=300, cwd=str(root), env=child_env)
    assert proc.returncode == 0, f"扫描子进程失败：{proc.stderr[-600:]}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return payload


def _rows_by(sweep_result, path, method=None):
    return [r for r in sweep_result["rows"]
            if r[1] == path and (method is None or r[0] == method)]


def test_bare_environment_is_really_bare(sweep_result):
    """前提反证：子进程必须真的处在一个「什么都没配」的世界里。

    这条不是装饰。上一版 BASE_DIR 因前缀写错仍指向真实仓库根，启动时把部署树的
    `.env` 读了进来 —— 扫描于是测的是「已鉴权的部署实例」，而它宣称测的是
    「新用户的第一次」。**判据先要确认自己在测哪个世界，再去数状态码。**
    """
    routes = sweep_result["routes"]
    hits = sweep_result["hits"]
    world = sweep_result["world"]
    assert routes > 100, f"路由数异常少（{routes}），扫描没生效"
    assert hits > 60, f"实际分类 {hits} 条，太少，判据没有射程"
    counts = sweep_result["counts"]
    assert sum(counts.values()) == hits, f"分类计数没有覆盖全部请求：{counts} / {hits}"
    assert counts["validation_rejected"] > 0, "没有单列 422；扫描可能又把验证拒绝算成已执行"
    assert counts["exercised"] > counts["validation_rejected"], f"真正执行的业务路由太少：{counts}"
    assert world["auth_enabled"] is False, (
        f"子进程里鉴权是开的（BASE_DIR={world['base_dir']}）—— "
        "扫描会拿到一片 401，而 401 不是缺陷，判据就此失真")
    assert world["backend"] == [False, "config_file_missing"], (
        f"子进程不是「未配置」形态：{world['backend']}（BASE_DIR={world['base_dir']}）")
    assert "site-packages" not in world["data_dir"] and "/dudu-mem0/" not in world["data_dir"], (
        f"DATA_DIR 落在真实部署/包目录里：{world['data_dir']} —— 扫描会写活数据")
def test_no_route_returns_500_when_backend_is_not_configured(sweep_result):
    """零凭据首跑：任何路由都不许回 500。

    503 是**正确**的（后端确实没配，且指引写得清楚），本条只禁 500 与
    「非 503 的 5xx」与异常。500 的含义是「服务端自己炸了」，而这里唯一的
    成因是「用户还没配置」—— 把「还没配」报成「我坏了」，调用方就再也分不出
    「等一下再来」与「得有人去修」。
    """
    rows = sweep_result["rows"]
    bad = [f"{m} {u} -> {code} {txt[:110]}"
           for m, u, code, txt, verdict in rows
           if verdict == "exercised" and (
               code == "EXC" or (isinstance(code, int) and code >= 500 and code != 503)
           )]
    assert not bad, "零凭据首跑出现 5xx/异常：\n  " + "\n  ".join(bad)
def test_503_details_stay_actionable(sweep_result):
    """503 不许退化成裸异常。

    本仓的 503 正文带四要素：缺什么 / 去哪配 / 怎么查 / 零凭据替代路径。
    这是它比同类项目做得好的地方，也是**必须守住**的地方 —— 一旦有人把
    `str(e)` 直接塞进 detail，这条会红。
    """
    rows = sweep_result["rows"]
    got = [r for r in rows if r[2] == 503 and r[4] == "exercised"]
    assert len(got) >= 3, f"只扫到 {len(got)} 条 503，环境可能不是未配置形态"
    for m, u, _code, txt, _verdict in got:
        assert "mem0_config_local" in txt or "未就绪" in txt, (
            f"{m} {u} 的 503 没告诉调用方缺什么：{txt[:120]}")
        assert "/health" in txt, f"{m} {u} 的 503 没告诉调用方怎么查：{txt[:160]}"
        assert "/add/raw" in txt, f"{m} {u} 的 503 没给零凭据替代路径：{txt[:160]}"
        assert "Traceback" not in txt, f"{m} {u} 把堆栈甩给了调用方"
def test_single_and_bulk_delete_agree_when_unconfigured(sweep_result):
    """两条删除链共用同一套三态契约，同一个环境必须给同一个判决。

    这是 P0-1 的靶心：修复前 `/delete` 回 500 failed、`/delete_all` 回 200 committed。
    同一条判据在同一个文件的两个函数里长出了两种行为 —— 相隔 426 行。
    """
    rows = sweep_result["rows"]
    single = [r for r in rows if r[1] == "/delete" and r[0] == "DELETE"]
    bulk = [r for r in rows if r[1] == "/delete_all"]
    assert single and bulk, "没扫到删除路由，夹具变了"
    assert single[0][2] == 200, f"零凭据下单条删除回 {single[0][2]}：{single[0][3][:120]}"
    assert bulk[0][2] == 200, f"零凭据下全量删除回 {bulk[0][2]}：{bulk[0][3][:120]}"


def test_high_risk_routes_reach_business_logic(sweep_result):
    """高风险面不得拿 422 冒充覆盖；每条都必须进业务逻辑。"""
    lookup = {(r[0], r[1]): r for r in sweep_result["rows"]}
    rejected = []
    for method, path in sweep_result["high_risk"]:
        row = lookup.get((method, path))
        if row is None or row[4] != "exercised":
            rejected.append(f"{method} {path}: {row}")
    assert not rejected, "高风险路由没有进入业务逻辑：\n  " + "\n  ".join(rejected)


def _production_mem0_config(*, embed_key="sk-live-a8f3c921d7e4",
                            llm_key="sk-live-b7e2d405c8f1"):
    """与 shipped example / Memory.from_config 相同的嵌套配置形状。"""
    return {
        "llm": {
            "provider": "openai",
            "config": {
                "model": "probe-llm",
                "api_key": llm_key,
                "openai_base_url": "https://llm.invalid/v1",
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": "probe-embedding",
                "api_key": embed_key,
                "openai_base_url": "https://embed.invalid/v1",
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": "probe",
                "path": "./data/qdrant",
                "embedding_model_dims": 1024,
            },
        },
    }


def _reset_mem0_singleton(monkeypatch, mr):
    monkeypatch.setattr(mr, "m", None)
    monkeypatch.delattr(mr.sys, "_aidumem_singleton", raising=False)


def test_production_shaped_config_drives_memory_from_config(tmp_path, monkeypatch):
    """真实嵌套配置必须原样到达 ``Memory.from_config``；成功路径不能被误降级。"""
    cfg = tmp_path / "mem0_config_local.json"
    expected = _production_mem0_config()
    cfg.write_text(json.dumps(expected), encoding="utf-8")

    import ducky.mem0_runtime as mr

    seen = []
    fake_mem = object()

    class _Memory:
        @staticmethod
        def from_config(got):
            seen.append(got)
            return fake_mem

    monkeypatch.setattr(mr, "mem0_config_path", lambda: str(cfg))
    monkeypatch.setattr(mr, "Memory", _Memory)
    monkeypatch.setattr(mr, "_assert_vector_store_inside_sandbox", lambda _cfg: None)
    monkeypatch.setattr(mr, "_clear_qdrant_lock", lambda: None)
    monkeypatch.setattr(mr, "_patch_usage_tracking", lambda _mem: {"ok": True, "patches": {}})
    _reset_mem0_singleton(monkeypatch, mr)

    assert mr.get_memory() is fake_mem
    assert seen == [expected], "加载器没有把生产形状原样交给 Memory.from_config"
    assert mr.mem0_backend_configured() == (True, "configured")


def test_production_shaped_init_failure_keeps_real_failure_type(tmp_path, monkeypatch):
    """生产形状配好后 ``Memory.from_config`` 失败，只能是普通 503，不能冒充未配置。"""
    cfg = tmp_path / "mem0_config_local.json"
    cfg.write_text(json.dumps(_production_mem0_config()), encoding="utf-8")

    import ducky.mem0_runtime as mr

    class _BrokenMemory:
        @staticmethod
        def from_config(_cfg):
            raise RuntimeError("qdrant connection refused")

    monkeypatch.setattr(mr, "mem0_config_path", lambda: str(cfg))
    monkeypatch.setattr(mr, "Memory", _BrokenMemory)
    monkeypatch.setattr(mr, "_assert_vector_store_inside_sandbox", lambda _cfg: None)
    monkeypatch.setattr(mr, "_clear_qdrant_lock", lambda: None)
    _reset_mem0_singleton(monkeypatch, mr)

    with pytest.raises(HTTPException) as got:
        mr.get_memory()
    assert got.value.status_code == 503
    assert not isinstance(got.value, mr.Mem0NotConfiguredError), (
        "已配置后端的初始化故障被改写成未配置，删除链会错误放行")
    assert mr.is_backend_not_configured(got.value) is False


def test_configured_but_broken_backend_still_counts_as_failure(tmp_path, monkeypatch):
    """**承重负向对照**：生产形状真配置存在时，后端故障不得变成「未启用」。"""
    cfg = tmp_path / "mem0_config_local.json"
    cfg.write_text(json.dumps(_production_mem0_config()), encoding="utf-8")

    import ducky.mem0_runtime as mr

    monkeypatch.setattr(mr, "mem0_config_path", lambda: str(cfg))
    configured, reason = mr.mem0_backend_configured()
    assert configured is True, "配了真凭据却被判为未配置 —— 降级判据过宽，F-02 会复活"
    assert reason == "configured"
    assert mr.is_backend_not_configured(RuntimeError("connection refused")) is False


def test_configured_backend_down_is_still_a_failure(tmp_path, monkeypatch):
    """**变异探针逼出来的一条**：配了真凭据、后端当场抛 503，仍必须算失败。

    上一轮跑变异探针时发现：把判据改成「看异常是不是 HTTPException(503)」，
    本文件**全绿**。而那个改法恰恰是 F-02 复活的通路 ——
    `get_memory()` 把「配置文件不存在」「凭据无效」「Qdrant 连不上」「代理坏了」
    四种成因**全部**包成 503。拿状态码当判据，等于宣布
    「凡是后端出问题，都当它没启用」：删除会报成功，而向量点留着，
    已删内容照旧可召回。

    原有的负向对照只喂了一个裸 RuntimeError，太温和，测不到这条通路。
    这里造出真实形态：配置是真的、异常是 503。
    """
    cfg = tmp_path / "mem0_config_local.json"
    cfg.write_text(json.dumps(_production_mem0_config()), encoding="utf-8")
    import importlib

    mr = importlib.import_module("ducky.mem0_runtime")
    monkeypatch.setattr(mr, "mem0_config_path", lambda: str(cfg))
    assert mr.mem0_backend_configured()[0] is True
    # 真实形态：后端已配置，但初始化抛 503（Qdrant 不可达 / 代理坏了 / 凭据被拒）
    boom = HTTPException(status_code=503, detail="记忆写入依赖的向量后端尚未就绪：嵌入服务地址不可达")
    assert mr.is_backend_not_configured(boom) is False, (
        "已配置后端的 503 被判成「未启用」—— 删除会报成功而向量点留着，F-02 复活。"
        "判据必须看世界的事实（配置文件与凭据），不能看状态码。")


def test_incomplete_or_flat_config_is_not_silently_treated_as_unconfigured(tmp_path, monkeypatch):
    """错误/不完整形状必须进入初始化失败，不能因 key 看起来像样例就跳过向量层。"""
    import ducky.mem0_runtime as mr

    cases = [
        {
            "embedder": {"api_key": "YOUR_EMBEDDING_API_KEY"},
            "llm": {"api_key": "YOUR_LLM_API_KEY"},
        },
        {
            "embedder": {"provider": "openai", "config": {}},
            "llm": {"provider": "openai", "config": {"api_key": "YOUR_LLM_API_KEY"}},
        },
    ]
    for index, payload in enumerate(cases):
        cfg = tmp_path / f"bad-shape-{index}.json"
        cfg.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(mr, "mem0_config_path", lambda cfg=cfg: str(cfg))
        assert mr.mem0_backend_configured() == (True, "configured"), (
            f"错误形状被当成未配置：{payload}")
        assert mr._load_mem0_config() == payload


def test_placeholder_production_config_raises_only_typed_absence(tmp_path, monkeypatch):
    """完整 shipped-example 形状、两把 key 都未替换时，才允许产生专用未配置类型。"""
    import ducky.mem0_runtime as mr

    cfg = tmp_path / "mem0_config_local.json"
    cfg.write_text(json.dumps(_production_mem0_config(
        embed_key="YOUR_EMBEDDING_API_KEY",
        llm_key="YOUR_LLM_API_KEY",
    )), encoding="utf-8")
    monkeypatch.setattr(mr, "mem0_config_path", lambda: str(cfg))

    assert mr.mem0_backend_configured() == (False, "placeholder_credentials")
    with pytest.raises(mr.Mem0NotConfiguredError) as got:
        mr._load_mem0_config()
    assert got.value.reason == "placeholder_credentials"


def test_unreadable_config_is_a_real_failure_not_an_absence(tmp_path, monkeypatch):
    """文件在但读不了 = 部署坏了，不是没配。必须 fail-closed。"""
    cfg = tmp_path / "mem0_config_local.json"
    cfg.write_text("{ this is not json", encoding="utf-8")
    import importlib

    mr = importlib.import_module("ducky.mem0_runtime")
    monkeypatch.setattr(mr, "mem0_config_path", lambda: str(cfg))
    configured, reason = mr.mem0_backend_configured()
    assert configured is True and reason == "config_unreadable", (
        "坏掉的配置文件被当成了「没配」—— 那等于让删除在配置损坏时报告成功")


def test_delete_chains_skip_mem0_only_for_the_typed_absence():
    """结构守卫：两条链只能捕获专用类型；不得用状态码、字符串或配置探针降级。"""
    import ast

    root = pathlib.Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "ducky" / "wal_engine.py").read_text(encoding="utf-8"))
    targets = {"cascade_delete_memory", "cascade_delete_all"}
    checked = set()

    for fn in [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name in targets]:
        source = ast.get_source_segment(
            (root / "ducky" / "wal_engine.py").read_text(encoding="utf-8"), fn) or ""
        assert "is_backend_not_configured" not in source, f"{fn.name} 又改回事后配置探针"
        assert ".status_code" not in source, f"{fn.name} 用 HTTP 状态码猜未配置"

        def _calls_outside_nested_try(statements):
            calls = []

            class _Visitor(ast.NodeVisitor):
                def visit_Try(self, _node):
                    return

                def visit_Call(self, node):
                    if getattr(node.func, "id", "") == "get_memory":
                        calls.append(node)
                    self.generic_visit(node)

            visitor = _Visitor()
            for statement in statements:
                visitor.visit(statement)
            return calls

        matching = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Try) and _calls_outside_nested_try(node.body):
                matching.append(node)
        assert len(matching) == 1, f"{fn.name} 的后端获取边界不是唯一 try：{len(matching)}"
        names = []
        for handler in matching[0].handlers:
            typ = handler.type
            if isinstance(typ, ast.Name):
                names.append(typ.id)
            elif isinstance(typ, ast.Attribute):
                names.append(typ.attr)
            else:
                names.append("")
        assert names[:2] == ["Mem0NotConfiguredError", "Exception"], (
            f"{fn.name} 捕获顺序/类型变了：{names}")
        checked.add(fn.name)

    assert checked == targets


def test_no_try_swallows_a_503_into_a_500():
    """P5 元守卫：全仓不许有「try 内取后端 + except Exception 抛 500 + 无透传」的块。

    本轮扫出 1 处（`routes_v8.py` 的 /graduate），修完归零。这条守卫的作用不是
    记住那一处，而是**让下一个新增路由不能重犯** —— `except HTTPException: raise`
    这个惯用法仓里已有 6 处，漏的那一处恰好在新用户必经之路上：
    **惯用法存在不等于被一致套用**（SOP 铁律 17「修复的射程」的第三种形态）。
    """
    import ast   # pathlib 已在模块级，函数内再 import 会遮蔽它（守卫抓的就是这个）

    def _names(t):
        if t is None:
            return []
        if isinstance(t, ast.Tuple):
            out = []
            for x in t.elts:
                out += _names(x)
            return out
        return [getattr(t, "id", None) or getattr(t, "attr", None) or ""]

    def _walk_body(body):
        for st in body:
            yield from ast.walk(st)

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    files = sorted(root.glob("ducky/**/*.py")) + [root / "api_server.py", root / "mcp_server.py"]
    for f in files:
        if "version.py" in f.name:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            passthrough = any("HTTPException" in _names(h.type) for h in node.handlers)
            touches_backend = any(
                isinstance(x, ast.Call) and getattr(x.func, "id", "") in ("get_memory",)
                for x in _walk_body(node.body))
            makes_500 = any(
                "Exception" in _names(h.type) and any(
                    isinstance(c, ast.Call) and getattr(c.func, "id", "") == "HTTPException"
                    and c.args and getattr(c.args[0], "value", None) == 500
                    for c in _walk_body(h.body))
                for h in node.handlers)
            if touches_backend and makes_500 and not passthrough:
                offenders.append(f"{f.relative_to(root)}:{node.lineno}")
    assert not offenders, (
        "这些 try 块会把后端的 503 吞成 500，调用方分不清「还没配好（可重试）」与"
        f"「服务端坏了（要人查）」：{offenders} —— "
        "修法是在 except Exception 之前加一句 `except HTTPException: raise`")


# ── 区分力说明（本轮变异探针实测）──────────────────────────────────────────
#
# 1) 删掉任一删除链的 `except Mem0NotConfiguredError` →
#    test_delete_chains_skip_mem0_only_for_the_typed_absence 与零凭据路由用例变红；
# 2) 删掉 routes_v8 graduate 的 `except HTTPException: raise` →
#    test_no_route_returns_500_when_backend_is_not_configured 变红；
# 3) 把专用类型捕获改成 `HTTPException` 或恢复事后配置探针 →
#    结构守卫、生产形状初始化失败和两条删除链故障注入同时变红
#    —— 既防漏修，也防「修过头」。

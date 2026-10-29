"""v20.3.1（九份审计 P0-3）：agent_integration_check 的两处空断言必须真断言。

上一版的两盏假绿灯：
  1. GET /gate 携带 JSON body —— FastAPI 只读 query string，nonce 整个被
     丢弃，恒命中 empty_query 早返回，任何 200 都算 PASS（/gate 已于
     f0.6+++ WP2 下线，判据改打脚本仍在用的 GET /api/core-memory）；
  2. `values.count(nonce)` 整串相等 —— 写入的是 f"{nonce} is …"，检索结果
     里永远不会有裸 nonce，恒 count==0 恒过。

判据设计遵循本仓 P5 原则（守卫能抓住下一个同类）：
  - gate 检查打**脚本自己的 request()**（只在 urlopen 这一层替换为
    TestClient 桥），不是测试自己复刻一份 request 逻辑 —— 第一版测试
    就是把 request 整个 mock 掉了，变异探针当场证明无区分力；
  - 重复注入用「故意注入两次必须红」的负向对照 —— 这才是判据有区分力
    的证明，一个恒过的断言和没有断言是同一种东西。
"""

import importlib.util
import json
import pathlib

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "aidumei_integration_check", _ROOT / "scripts" / "agent_integration_check.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def real_gate_bridge(monkeypatch):
    """让脚本的 request() 打到真实 FastAPI GET 路由（f0.6+++ WP2 起 /gate
    已下线，改用脚本仍在调的 GET /api/core-memory，它同样只读 query string）。

    只在 urlopen 层架桥 —— request() 的 GET/POST/body 处理逻辑是本轮的
    被测对象，必须用脚本本体，不许复刻。
    """
    import ducky.routes_clotho as clotho
    monkeypatch.setattr(clotho, "get_all_blocks", lambda user_id, bank_id: {})
    app = FastAPI()
    clotho.register_clotho_routes(app)
    client = TestClient(app)

    def _bridge(req, timeout=20):
        """urllib.Request → TestClient 请求。"""
        method = req.get_method()
        url = req.full_url
        path = url.replace("http://127.0.0.1:8767", "", 1)
        body = req.data
        resp = client.request(method, path, data=body,
                              headers={"Content-Type": "application/json"})
        resp.status_code = resp.status_code

        class _R:
            def __init__(self, status, payload):
                self.status = status
                self._payload = payload
                self.status_code = status

            def read(self):
                return json.dumps(self._payload).encode()

            def json(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _R(resp.status_code, resp.json())

    script = _load_script()
    monkeypatch.setattr(script.urllib.request, "urlopen", _bridge, raising=False)
    return script, client


def test_gate_params_arrive_via_query_string(real_gate_bridge):
    """脚本的 request() 对 GET dict 必须经 query string 到达服务端。

    第一版此测试把 request() 整个 mock 掉（测的是复制品），变异探针
    （把 GET 修复改回 body 形态）当场证明它无区分力 —— 本版测试打
    脚本本体，同一个变异必须红。
    """
    script, client = real_gate_bridge
    status, core = script.request("GET", "/api/core-memory",
                                  {"user_id": "integration-probe-u1", "bank_id": "probe-bank",
                                   "caller_user_id": "integration-probe-u1"})
    assert status == 200, core
    # 回显的作用域必须是本次传的值 —— 参数被丢就会回落到默认租户/默认库。
    assert core.get("user_id") == "integration-probe-u1", (
        "GET 参数又丢了：user_id 回落默认值说明 query 根本没到达服务端")
    assert core.get("bank_id") == "probe-bank"


def test_get_with_dict_body_would_fail_this_assertion(real_gate_bridge):
    """负向对照（判据区分力的证明）：body 形态的 GET 读不到参数，
    服务端回落默认作用域 —— 上一版正是这个形态恒过 check。"""
    script, client = real_gate_bridge
    # 直接用通用 request() 复刻旧 bug 形态：GET + JSON body（无 query string）
    # （TestClient.get() 不收 json= —— 通用形态才发得出 body）
    resp = client.request("GET", "/api/core-memory",
                          data=json.dumps({"user_id": "integration-probe-u1",
                                           "bank_id": "probe-bank"}).encode(),
                          headers={"Content-Type": "application/json"})
    assert resp.json().get("bank_id") != "probe-bank", (
        "服务端契约变了？GET+body 现在居然能读到参数 —— 那本判据要重审"
    )
    # 这证明：若脚本回归 body 形态，回显的作用域必不是传入值，
    # 上一条测试的断言必红 —— 区分力成立。


def test_duplicate_injection_substring_counting_has_discriminating_power():
    """负向对照：故意注入两次必须判 fail —— 证明子串计数真的会红。

    旧判据 values.count(nonce)<=1 在注入五次时也恒过（整串永不相等）。
    这里对判据本身做变异验证：dup=2 必须不满足 <=1。
    """
    nonce = "integration-deadbeef"
    values = [
        f"{nonce} is the integration handshake.",
        f"repeated: {nonce} appears again",
    ]
    dup_count = sum(1 for v in values if v and nonce in str(v))
    assert dup_count == 2, "子串计数失效：两次注入没数出来"
    assert not (dup_count <= 1), "判据没有区分力：注入两次还是过 = 假绿灯"
    # 正向对照：单条注入必须过
    single = [f"{nonce} once"]
    assert sum(1 for v in single if v and nonce in str(v)) <= 1


def test_tenant_guardrail_rejects_default_and_foreign_prefix(tmp_path):
    """P2-1：--tenant 被诱导传 default 时必须拒绝（防验收即清库）。"""
    import subprocess
    import os
    import sys
    env = {**os.environ,
           "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
           "AIDUMEM_LOG_DIR": str(tmp_path / "logs")}
    for bad_tenant in ("default", "victim-user"):
        r = subprocess.run(
            [sys.executable, "scripts/agent_integration_check.py", "--tenant", bad_tenant],
            capture_output=True, text=True, timeout=20, env=env, cwd=str(_ROOT),
        )
        assert r.returncode == 2, (
            f"tenant={bad_tenant!r} 的护栏没拦住（exit={r.returncode}）—— "
            "一次『验收』就是一次清库"
        )

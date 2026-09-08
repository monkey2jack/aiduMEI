"""v20.4.0-alpha · P0-1：/add 输入边界与全量写端点预算台账。

背景（六方外审复核结论）：v20.2.5 给 /search 加了 `query ≤ 10000 / limit ≤ 100`
之后，写入口只剩 /add 的 `messages` 裸奔 —— `ducky/api_models.py` 里唯一
无上限的自由文本载荷，且全仓没有全局 body 限制。GLM L-1 把 /add 与 /search
捆在一起报「均无上限」，复核实锤只有 /add 这半条（/search 早有上限）。

本文件钉四件事：
1. `messages`：str 超 50,000 字符 → 422；list/dict 序列化超 64 KiB → 422；边界值放行。
2. 全局 body 硬顶：Content-Length > 1 MiB → 413，且 413 在计数与安全头覆盖之内。
3. 台账：`ducky/write_endpoint_budgets.py` 登记全部写路由；未登记 → 红；
   登记了但路由消失 → 红；模型自由文本字段无上限且未登记豁免 → 红。
4. 台账守卫自身的变异自证：假路由/假豁免必须被抓出来。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def add_client(tmp_path, monkeypatch):
    import ducky.utils as utils
    from ducky.hot.add import register_add_routes

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    app = FastAPI()
    register_add_routes(app)
    return TestClient(app)


class TestMessagesBudget:
    """修复前：两条超限用例必须是红的（此前 422 不存在）。"""

    def test_messages_str_over_limit_rejected(self, add_client):
        r = add_client.post("/add", json={"messages": "字" * 100_000, "user_id": "cap_u"})
        assert r.status_code == 422, f"10 万字 messages 必须 422，实得 {r.status_code}"

    def test_messages_list_serialized_over_limit_rejected(self, add_client):
        big = [{"role": "user", "content": "x" * 70_000}]
        r = add_client.post("/add", json={"messages": big, "user_id": "cap_u"})
        assert r.status_code == 422, f"序列化超 64 KiB 的 list 必须 422，实得 {r.status_code}"

    def test_messages_dict_serialized_over_limit_rejected(self, add_client):
        big = {"role": "user", "content": "x" * 70_000}
        r = add_client.post("/add", json={"messages": big, "user_id": "cap_u"})
        assert r.status_code == 422, f"序列化超 64 KiB 的 dict 必须 422，实得 {r.status_code}"

    def test_messages_str_at_boundary_passes_validation(self, add_client):
        """边界值 50,000 字符必须过校验（业务结果如何不归本用例管）。"""
        r = add_client.post("/add", json={"messages": "字" * 50_000, "user_id": "cap_u"})
        assert r.status_code != 422, f"边界值不许被误杀：{r.status_code} {r.text[:200]}"

    def test_messages_normal_payload_passes_validation(self, add_client):
        r = add_client.post("/add", json={"messages": "今天开了三小时会", "user_id": "cap_u"})
        assert r.status_code != 422, f"正常载荷不许被误杀：{r.status_code}"


class TestGlobalBodyCap:
    """全局 Content-Length 硬顶 1 MiB → 413。

    体积闸注册在「计数（最外）→ 安全头 → 体积闸 → 鉴权 → 路由」的第三层：
    被挡的 413 必须照样被计数、照样带安全头 —— 与 v20.3.2 中间件顺序教训同源，
    断言把头也钉上。
    """

    def test_oversized_body_rejected_413_with_security_headers(self):
        from api_server import app

        client = TestClient(app)
        payload = b"x" * (1024 * 1024 + 1)
        r = client.post("/add", content=payload,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 413, f"1 MiB+1 字节必须 413，实得 {r.status_code}"
        assert "Content-Security-Policy" in r.headers, "413 必须带安全头（体积闸在安全头内层）"

    def test_normal_body_passes_body_cap(self):
        from api_server import app

        client = TestClient(app)
        r = client.post("/add", json={"messages": "小", "user_id": "cap_u"})
        assert r.status_code != 413, "正常体量不许被体积闸误杀"


class TestWriteEndpointBudgetLedger:
    """全量写端点普查：真实 app 路由表 ⇆ 台账 双向往返。"""

    def test_no_violations_on_real_app(self):
        from api_server import app
        from ducky.write_endpoint_budgets import find_budget_violations

        violations = find_budget_violations(app)
        assert violations == [], "写端点预算台账与真实路由表不符：\n  " + "\n  ".join(violations)

    def test_guard_detects_unregistered_write_route(self):
        """变异自证 A：多一条未登记写路由，守卫必须抓出来。"""
        from ducky.write_endpoint_budgets import find_budget_violations

        app = FastAPI()

        @app.post("/definitely/not/registered")
        def _rogue():  # pragma: no cover - 变异夹具本体
            return {"ok": True}

        violations = find_budget_violations(app)
        assert any("/definitely/not/registered" in v for v in violations), \
            f"未登记写路由没被抓到：{violations}"

    def test_guard_detects_uncapped_text_field(self):
        """变异自证 B：模型里出现无上限自由文本字段，守卫必须抓出来。"""
        from pydantic import BaseModel
        from ducky.write_endpoint_budgets import find_budget_violations

        class RoguePayload(BaseModel):
            big_free_text: str

        app = FastAPI()

        @app.post("/rogue/with-model")
        def _rogue(req: RoguePayload):  # pragma: no cover - 变异夹具本体
            return {"ok": True}

        violations = find_budget_violations(app)
        assert any("big_free_text" in v for v in violations), \
            f"无上限自由文本字段没被抓到：{violations}"

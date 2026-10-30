"""/stats 只回统计数字，不再把本域全部记忆正文整包下发。

此前 stats 以 limit=10000 拉整域，并把结果原样放在 "memories" 里返回：控制台
「心动」面板只读 total_memories，却每次都收到全部正文（可达数 MB），这个端点也成了
最便宜的整域导出口。
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def test_stats_counts_without_returning_memory_text(monkeypatch):
    import api_server

    # 自带凭据：不依赖前序用例是否在共享数据目录里留下口令（门禁开关）
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "stats-no-dump-token")
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    client = TestClient(api_server.app, headers={"Authorization": "Bearer stats-no-dump-token"})
    user = f"stats-{uuid.uuid4().hex[:8]}"
    secret = f"stats probe {uuid.uuid4().hex}"
    added = client.post("/add", json={"messages": [{"role": "user", "content": secret}],
                                      "user_id": user, "caller_user_id": user, "infer": False})
    assert added.status_code == 200, added.text

    resp = client.get("/api/stats", params={"user_id": user, "caller_user_id": user})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_memories"] >= 1
    assert "memories" not in body
    assert secret not in resp.text

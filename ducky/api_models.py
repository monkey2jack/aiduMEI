"""
ducky.api_models — FastAPI 请求/响应模型（C 档从 api_server 抽出）
2026-07-21: /add 增加 async_mode 高速选项
"""

from pydantic import BaseModel, ConfigDict, Field

from ducky.utils import DEFAULT_USER_ID


class AddRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: str = ""
    user_id: str = DEFAULT_USER_ID
    metadata: dict = Field(default_factory=dict)
    # true=先回执后台落库；默认 false 保持同步语义（兼容旧调用方）
    async_mode: bool = False

class SearchRequest(BaseModel):
    query: str
    user_id: str = DEFAULT_USER_ID
    limit: int = 5


class SearchResponse(BaseModel):
    status: str = "ok"
    results: list = Field(default_factory=list)


class DeleteRequest(BaseModel):
    memory_id: str
    user_id: str = DEFAULT_USER_ID


class UpdateRequest(BaseModel):
    memory_id: str
    user_id: str = DEFAULT_USER_ID
    content: str = ""


class InjectContextRequest(BaseModel):
    # 新 facts 注入协议；user_content 保留兼容旧调用方。
    query: str = ""
    k: int = 5
    level: str = "L0"
    max_tokens: int = 1000
    user_content: str = ""
    assistant_content: str = ""
    user_id: str = DEFAULT_USER_ID

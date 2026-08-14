"""
ducky.api_models — FastAPI 请求/响应模型（C 档从 api_server 抽出）
2026-07-21: /add 增加 async_mode 高速选项
2026-08-13: /add 的 messages 兼容 str / list / dict 三种输入
"""

from typing import Any, Dict, List, Union

from pydantic import BaseModel, ConfigDict, Field

from ducky.utils import DEFAULT_USER_ID

# 上游 mem0 与 aiduMEM 的历史调用方混用了三种形态：
#   1) 纯文本字符串        → "今天开会"
#   2) JSON 字符串         → "[{\"role\":\"user\",\"content\":\"...\"}]"
#   3) OpenAI messages 数组 → [{"role":"user","content":"..."}]
# add.py 内部已有 isinstance 分支处理这三类，这里把模型放开，
# 避免 Pydantic 在进入业务逻辑前就把 list/dict 拒成 422。
# 类型收紧到常见形态，保留 Any 值以兼容历史调用方的自由字段。
Messages = Union[str, List[Dict[str, Any]], Dict[str, Any]]


class AddRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: Messages = ""
    user_id: str = DEFAULT_USER_ID
    metadata: dict = Field(default_factory=dict)
    # true=先回执后台落库；默认 false 保持同步语义（兼容旧调用方）
    async_mode: bool = False

class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    query: str
    user_id: str = DEFAULT_USER_ID
    limit: int = 5
    # MCP 等调用方传的是 top_k；显式接收，避免被 Pydantic 静默丢弃
    # 导致调用方指定数量永远不生效（P2-1 审计发现）。
    top_k: int = 0
    # P0-4 时间窗口过滤（可选，兼容旧调用方）
    before: str = ""
    after: str = ""


class SearchResponse(BaseModel):
    status: str = "ok"
    results: list = Field(default_factory=list)


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    memory_id: str
    user_id: str = DEFAULT_USER_ID


class DeleteAllRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    # 🔴P0-3: 必须显式指定 user_id，缺失拒绝执行
    user_id: str = ""
    # 清空 default 租户必须显式传递 confirm=True
    confirm: bool = False


class UpdateRequest(BaseModel):
    # 🟡P0-2：放开额外字段并兼容旧调用方传 data 的写法，
    # 避免 data 被 Pydantic 静默丢弃后把记忆更新成空串。
    model_config = ConfigDict(extra="allow")

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

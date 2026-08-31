"""
ducky.api_models — FastAPI 请求/响应模型（C 档从 api_server 抽出）
2026-07-21: /add 增加 async_mode 高速选项
2026-08-13: /add 的 messages 兼容 str / list / dict 三种输入
"""

import re
from typing import Any, Dict, List, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ducky.utils import DEFAULT_USER_ID
from ducky.bank_contract import DEFAULT_BANK_ID

# 上游 mem0 与 aiduMEM 的历史调用方混用了三种形态：
#   1) 纯文本字符串        → "今天开会"
#   2) JSON 字符串         → "[{\"role\":\"user\",\"content\":\"...\"}]"
#   3) OpenAI messages 数组 → [{"role":"user","content":"..."}]
# add.py 内部已有 isinstance 分支处理这三类，这里把模型放开，
# 避免 Pydantic 在进入业务逻辑前就把 list/dict 拒成 422。
# 类型收紧到常见形态，保留 Any 值以兼容历史调用方的自由字段。
Messages = Union[str, List[Dict[str, Any]], Dict[str, Any]]


# v20.1.1（N-2，外审建议采纳）：metadata 形态白名单。
# 顶层 extra="allow" 是**有意的兼容设计**（老调用方多传字段不炸，见
# SearchRequest 的 top_k 教训），保持不动；收紧的是 metadata **内容形态**：
# 它一路透传进 facts / 向量 payload，超长键、嵌套炸弹、二进制垃圾都会
# 永久落库。键允许中英数与 . - _（含 CJK——生产存量有中文键的自由），
# 上限的依据是承载面：qdrant payload 与 facts 列的合理载荷，不是拍脑袋
# 的「看着差不多」——单值 4KB ≈ 一条 verbatim 原文的上限量级，总量 16KB
# ≈ 单条记忆全部旁路元数据的 4 倍余量。
_METADATA_KEY_RE = re.compile(r"^[\w\u4e00-\u9fff.\-]{1,64}$")
_METADATA_MAX_KEYS = 32
_METADATA_MAX_VALUE_CHARS = 4096
_METADATA_MAX_TOTAL_CHARS = 16384
_METADATA_MAX_DEPTH = 2


def _metadata_depth(v: Any, depth: int = 0) -> int:
    if isinstance(v, dict):
        return max([depth + 1] + [_metadata_depth(x, depth + 1) for x in v.values()])
    if isinstance(v, (list, tuple)):
        return max([depth + 1] + [_metadata_depth(x, depth + 1) for x in v])
    return depth


class AddRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: Messages = ""
    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID
    metadata: dict = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _metadata_shape(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("metadata 必须是对象")
        if len(v) > _METADATA_MAX_KEYS:
            raise ValueError(f"metadata 键数 {len(v)} 超上限 {_METADATA_MAX_KEYS}")
        total = 0
        for k, val in v.items():
            if not isinstance(k, str) or not _METADATA_KEY_RE.match(k):
                raise ValueError(f"metadata 键名不合法: {str(k)[:80]!r}（允许中英数与 . - _，长度 1-64）")
            piece = str(val)
            if len(piece) > _METADATA_MAX_VALUE_CHARS:
                raise ValueError(f"metadata 键 {k!r} 的值长 {len(piece)} 超单值上限 {_METADATA_MAX_VALUE_CHARS}")
            total += len(k) + len(piece)
        if total > _METADATA_MAX_TOTAL_CHARS:
            raise ValueError(f"metadata 总载荷 {total} 超上限 {_METADATA_MAX_TOTAL_CHARS}")
        if _metadata_depth(v) > _METADATA_MAX_DEPTH:
            raise ValueError(f"metadata 嵌套深度超上限 {_METADATA_MAX_DEPTH}")
        return v
    # true=先回执后台落库；默认 false 保持同步语义（兼容旧调用方）
    async_mode: bool = False
    # v20：显式的免抽取写入开关（mem0 的 infer 参数）。
    # infer=true（默认，生产语义）：LLM 抽取事实后落库。
    # infer=false：跳过 LLM，原文规范化直写；写入链路变成
    #   「同输入 → 同输出」的确定性通路。仓内 speed/pipeline.py:127
    #   的 fastpath 早就在用 infer=False，只是 /add 从未暴露。
    # 这是**公开契约参数**，不是隐藏的 benchmark 模式：调用方显式传，
    # 服务端在响应里回显（见 hot/add.py），无法被静默忽略。
    infer: bool = True
    # v20.3 user-audit remediation: make client retries safely replayable.
    idempotency_key: str = Field(default="", min_length=0, max_length=128)

class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    # v20.2.5（用户实测 Y-NEW2）：此前 limit 无任何约束 —— 实测 limit=-5 与
    # limit=999999 都直通 HTTP 200。当前数据量小所以没炸，量大了
    # limit=999999 就是一次全表扫描。query 同理：10 万字不拒、1.166s 才返回。
    # 校验落在**模型**上而不是路由里：模型是所有调用方的共同入口，
    # 放路由里就得每条路由各写一遍，而漏掉的那条不会有人发现。
    query: str = Field(default=..., max_length=10000)
    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID
    limit: int = Field(default=5, ge=1, le=100)
    # MCP 等调用方传的是 top_k；显式接收，避免被 Pydantic 静默丢弃
    # 导致调用方指定数量永远不生效（P2-1 审计发现）。
    top_k: int = 0
    # P0-4 时间窗口过滤（可选，兼容旧调用方）
    before: str = ""
    after: str = ""


class SearchResponse(BaseModel):
    # v20 P0-4：必须 extra="allow"。此前严格模式下 FastAPI 按本模型过滤
    # 响应，_workspace_hit 和错误路径的 detail 一直被静默剥掉——调用方
    # 拿到 status:"error" 却看不到 detail。现放行 _recall_path/_rerank/
    # detail 等可观测字段。
    model_config = ConfigDict(extra="allow")

    status: str = "ok"
    results: list = Field(default_factory=list)


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    memory_id: str
    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID


class DeleteAllRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    # 🔴P0-3: 必须显式指定 user_id，缺失拒绝执行
    user_id: str = ""
    bank_id: str = DEFAULT_BANK_ID
    # 清空 default 租户必须显式传递 confirm=True
    confirm: bool = False


class TombstoneRestoreRequest(BaseModel):
    """🪦 tombstone 恢复请求（v19.4.0 Mímir 借鉴 B3）"""
    model_config = ConfigDict(extra="allow")

    tombstone_id: int
    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID

class GovernanceReviewRequest(BaseModel):
    """🏛️ 治理管线人审请求（v19.4.0 Mímir 借鉴 B1）"""
    model_config = ConfigDict(extra="allow")

    candidate_id: int
    decision: str  # approve | reject
    reason: str = ""
    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID

class OpinionSetRequest(BaseModel):
    """🧭 信念层写入请求（v19.4.0 Mímir 借鉴 B6）"""
    model_config = ConfigDict(extra="allow")

    fact_id: int
    stance: str  # support | oppose | neutral
    confidence: float = 0.5
    evidence_ids: list = []
    source: str  # 证据来源标识（必填，聚合按来源去重）
    owner: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID


class UpdateRequest(BaseModel):
    # 🟡P0-2：放开额外字段并兼容旧调用方传 data 的写法，
    # 避免 data 被 Pydantic 静默丢弃后把记忆更新成空串。
    model_config = ConfigDict(extra="allow")

    memory_id: str
    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID
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
    bank_id: str = DEFAULT_BANK_ID

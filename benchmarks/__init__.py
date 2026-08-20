"""benchmarks — aiduMEI 可复现评测管线（v20.0 P0-3）。

这里没有任何「跑过分」的宣称：管线先于分数存在。协议（PROTOCOL.md）
锁数据、模型、judge、prompt、seed 与哈希；adapter 走真实 HTTP 契约；
schemas 在装载时校验数据形状；run 负责 smoke 与正式运行的编排与留证。
"""
from benchmarks.adapter import AdapterError, AiduMEIBenchmarkAdapter
from benchmarks.schemas import SchemaError, validate_locomo, validate_longmemeval

__all__ = [
    "AdapterError",
    "AiduMEIBenchmarkAdapter",
    "SchemaError",
    "validate_locomo",
    "validate_longmemeval",
]

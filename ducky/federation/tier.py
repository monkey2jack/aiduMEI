"""
ducky.federation.tier — 分层记忆与差异化衰减
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

「不是所有记忆都该活一样长。」

    episodic   事件流水    30 天   时间优先
    semantic   配置/偏好   180 天  语义相似度优先
    procedural 铁律/范式   永不衰减 精确匹配优先

衰减 ≠ 删除
    到期只降权（recall 排序沉底），不删行、不改 archived。
    procedural 层零衰减，铁律永远满权重——这是硬保证。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

# ── 三层定义 ────────────────────────────────────────────
EPISODIC = "episodic"
SEMANTIC = "semantic"
PROCEDURAL = "procedural"

VALID_TIERS = (EPISODIC, SEMANTIC, PROCEDURAL)

# 各层 TTL（天）；None = 永不衰减
TIER_TTL_DAYS: dict[str, int | None] = {
    EPISODIC: 30,
    SEMANTIC: 180,
    PROCEDURAL: None,
}

# 各层召回基础权重（procedural 最高，保证铁律优先）
TIER_WEIGHT: dict[str, float] = {
    PROCEDURAL: 1.00,
    SEMANTIC: 0.85,
    EPISODIC: 0.70,
}

# category 关键词 → tier 的推断规则（命中即返回，顺序敏感）
_CATEGORY_RULES: tuple[tuple[str, str], ...] = (
    # 铁律 / 规范 / 范式 → procedural
    ("铁律", PROCEDURAL),
    ("iron_rule", PROCEDURAL),
    ("规范", PROCEDURAL),
    ("原则", PROCEDURAL),
    ("pattern", PROCEDURAL),
    ("范式", PROCEDURAL),
    ("协议", PROCEDURAL),
    ("skill", PROCEDURAL),
    # 事件 / 日记 / 会话 → episodic
    ("event", EPISODIC),
    ("事件", EPISODIC),
    ("日记", EPISODIC),
    ("diary", EPISODIC),
    ("session", EPISODIC),
    ("对话", EPISODIC),
    ("观察", EPISODIC),
    ("observation", EPISODIC),
    # 配置 / 偏好 / 知识 → semantic
    ("config", SEMANTIC),
    ("配置", SEMANTIC),
    ("偏好", SEMANTIC),
    ("pref", SEMANTIC),
    ("profile", SEMANTIC),
    ("learning", SEMANTIC),
    ("reference", SEMANTIC),
)


def normalize_tier(tier: str | None) -> str:
    """把任意输入规整成合法 tier，非法值退回 semantic（最中性的一层）。"""
    value = (tier or "").strip().lower()
    return value if value in VALID_TIERS else SEMANTIC


def infer_tier(category: str = "", fact_key: str = "", fact_value: str = "") -> str:
    """从 category / key / value 推断记忆层级。推断不出时返回 semantic。"""
    haystack = f"{category} {fact_key} {fact_value}".lower()
    for needle, tier in _CATEGORY_RULES:
        if needle in haystack:
            return tier
    return SEMANTIC


def decay_deadline(tier: str, recorded_at: datetime | None = None) -> str | None:
    """计算衰减到期时刻（ISO 字符串）。procedural 返回 None（永不衰减）。"""
    tier = normalize_tier(tier)
    ttl = TIER_TTL_DAYS[tier]
    if ttl is None:
        return None
    base = recorded_at or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(days=ttl)).isoformat()


def tier_weight(tier: str) -> float:
    """该层的召回基础权重。"""
    return TIER_WEIGHT[normalize_tier(tier)]


def decay_factor(tier: str, age_days: float) -> float:
    """
    分层衰减系数 ∈ (0, 1]。

    procedural：恒为 1.0（铁律零衰减）。
    其余层：以 TTL 为半衰期做指数衰减，到 TTL 时约 0.5，
    永不归零——旧事实只沉底，不消失。
    """
    tier = normalize_tier(tier)
    ttl = TIER_TTL_DAYS[tier]
    if ttl is None:
        return 1.0
    age = max(0.0, float(age_days))
    return math.exp(-math.log(2) * age / ttl)


def score_multiplier(tier: str, age_days: float) -> float:
    """层权重 × 衰减系数 = 该事实在综合排序中的最终乘子。"""
    return tier_weight(tier) * decay_factor(tier, age_days)

"""ducky.salience.config — 衰减常量 / Lane 关键词"""
from __future__ import annotations

import math
import os

from ducky.utils import SALIENCE_DB, get_salience_conn  # noqa: F401 — re-export 兼容


def _manifest_num(key: str, fallback: float) -> float:
    """🔴10：从 manifest.json 的 config 段读取可配置项默认值，环境变量优先。

    此前 manifest 的 salience_half_life_days / salience_floor 等只是摆设，代码全硬编码，
    「可配置」卖点失效。现在真正读取：环境变量 AIDUMEM_<KEY大写> > manifest default > fallback。
    """
    env = os.getenv(f"AIDUMEM_{key.upper()}")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        import json
        mpath = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "manifest.json")
        with open(mpath, encoding="utf-8") as f:
            manifest = json.load(f)
        # manifest 里 config 段位于 capabilities.config
        cfg = (manifest.get("capabilities", {}) or {}).get("config", {}) or {}
        node = cfg.get(key)
        if isinstance(node, dict) and "default" in node:
            return float(node["default"])
    except Exception:
        # safe-ignore: 配置读取失败回退默认值，无需上抛
        pass
    return fallback


DECAY_HALF_LIFE_DAYS = max(_manifest_num("salience_half_life_days", 30), 0.1)  # 半衰期（天），下限保护防止除零
DECAY_RATE = math.log(2) / DECAY_HALF_LIFE_DAYS  # ≈ 0.0231 / 天
SALIENCE_FLOOR = _manifest_num("salience_floor", 0.2)  # 低于此值 + 闲置 → 踢出
IDLE_EVICT_DAYS = 30            # 闲置超时踢出
ACCESS_BOOST = 0.1              # 每次访问 boost

# ── v8.3.0 Lane 感知衰减 ──
LANE_DECAY_MULTIPLIER = {
    "identity":    0.0,    # 身份铁律不衰减
    "preference":  0.0,    # 偏好铁律不衰减
    "procedural":  0.3,    # 操作步骤 30% 慢衰减
    "rule":        0.5,    # 规则 50% 慢衰减
    "lesson":      0.5,    # 踩坑教训 50% 慢衰减，需要动态闭环验证
    "evidence":    0.7,    # 证据 70% 衰减
    "knowledge":   1.0,    # 知识正常衰减
    "emotion":     1.5,    # 情绪 150% 快衰减
    "general":     1.0,
}
DEFAULT_LANE = "general"

# ── v8.3.0 Lane 自动检测关键词 ──
LANE_KEYWORDS = {
    "identity":    ["我是", "我叫", "我的名字", "我住在", "我出生", "我的生日", "我来自"],
    "preference":  ["喜欢", "爱", "偏好", "最爱", "讨厌", "不喜欢", "习惯"],
    "procedural":  ["步骤", "先", "然后", "配置", "设置", "启动", "运行", "执行", "命令"],
    "rule":        ["规则", "必须", "禁止", "不能", "一定要", "铁律", "绝不"],
    "lesson":      ["修复", "踩坑", "报错", "修复成功", "失败", "排查", "bug", "修好了", "错误", "故障"],
    "evidence":    ["发现", "测试", "验证", "结果", "数据显示", "实验", "确认"],
    "knowledge":   ["API", "端口", "版本", "服务器", "数据库", "文件", "路径", "代码"],
    "emotion":     ["开心", "难过", "生气", "想", "觉得", "感觉", "思念", "怀念", "讨厌", "郁闷", "吐槽"],
}

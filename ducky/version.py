"""
ducky.version — aiduMEM 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v15.1 Kalliope（卡利俄佩 · 缪斯之首）
    代码瘦身 · FTS去重 · legacy精简
    在 v15.0 官方通道的基础上做一轮内功修炼：
    legacy 808行瘦身 ~80行（FTS函数归一到 text_fts），
    消除 _compute_similarity / bare except / 过期 build 产物，
    让代码更干净、维护更轻松。
"""

SERVICE_VERSION = "15.1.0"
CODENAME = "Kalliope"
CODENAME_ZH = "卡利俄佩"

# 架构代号：接入宿主官方记忆通道的可移植记忆引擎
ARCHITECTURE = "Native Provider Bridge"

# 历史版本谱系（大版本代号，最新在前）
LINEAGE = (
    ("15.1", "Kalliope", "卡利俄佩", "代码瘦身 · FTS去重 · legacy精简"),
    ("15.0", "Iris", "伊里斯", "官方通道 · 惰性热载 · 静默归零"),
    ("14.0", "Aegis", "埃癸斯", "零硬编码 · 隐私护盾 · 开箱可部署"),
    ("13.0", "Pantheon", "万神殿", "多 Agent 联邦 · MoE 门控"),
    ("12.0", "Chronos", "克罗诺斯", "双时间轴有效期"),
    ("11.0", "Hyperion", "海伯利安", "线程本地连接池 · 性能纪元"),
    ("9.1", "Mnemosyne", "谟涅摩绪涅", "潮浪并忆 · 双策分档"),
)

# 完整版本字符串
FULL_VERSION = f"v{SERVICE_VERSION}-{CODENAME}"
DISPLAY_NAME = f"aiduMEM v{SERVICE_VERSION} {CODENAME}"

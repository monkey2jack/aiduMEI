"""
ducky.version — aiduMEM 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v14.0 Aegis（埃癸斯 · 神盾）
    零硬编码 · 隐私护盾 · 开箱可部署
    神盾护住的不是代码，是代码背后的人——
    所有身份、路径、密钥、词表全部外部注入，
    仓库里只留能力，不留主人的痕迹。
"""

SERVICE_VERSION = "14.0.1"
CODENAME = "Aegis"
CODENAME_ZH = "埃癸斯"

# 架构代号：环境注入式可移植记忆引擎
ARCHITECTURE = "Portable Zero-Hardcode Memory"

# 历史版本谱系（大版本代号，最新在前）
LINEAGE = (
    ("14.0", "Aegis", "埃癸斯", "零硬编码 · 隐私护盾 · 开箱可部署"),
    ("13.0", "Pantheon", "万神殿", "多 Agent 联邦 · MoE 门控"),
    ("12.0", "Chronos", "克罗诺斯", "双时间轴有效期"),
    ("11.0", "Hyperion", "海伯利安", "线程本地连接池 · 性能纪元"),
    ("9.1", "Mnemosyne", "谟涅摩绪涅", "潮浪并忆 · 双策分档"),
)

# 完整版本字符串
FULL_VERSION = f"v{SERVICE_VERSION}-{CODENAME}"
DISPLAY_NAME = f"aiduMEM v{SERVICE_VERSION} {CODENAME}"

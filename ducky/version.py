"""
ducky.version — aiduMEM 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v16.0 Opus Octopod (opus八爪鱼)
    三大核心吸收与重大突破：
    1. ConflictResolver: 属性与规则级显式冲突消解 (valid_to 失效降权)
    2. TreeMemory: 层级与树状节点表达 (node_path / 树状记忆图谱)
    3. SkillCrystallizer: 碎片记忆向标准化 Skill 候选项的自动演进结晶
"""

SERVICE_VERSION = "16.0.0"
FULL_VERSION = f"v{SERVICE_VERSION}"
CODENAME = "Opus Octopod"
CODENAME_ZH = "opus八爪鱼"
DISPLAY_NAME = f"aiduMEM {FULL_VERSION} · {CODENAME_ZH}"

# 架构代号：八爪延伸 · 自消解 · 树状图谱 · 经验结晶
ARCHITECTURE = "Self-Evolving Multi-Dimensional Memory OS"

# 历史版本谱系（大版本代号，最新在前）
LINEAGE = (
    ("16.0", "Opus Octopod", "opus八爪鱼", "冲突消解 · 树状记忆 · 技能结晶"),
    ("15.1", "Kalliope", "卡利俄佩", "代码瘦身 · FTS去重 · legacy精简"),
    ("15.0", "Iris", "伊里斯", "官方通道 · 惰性热载 · 静默归零"),
    ("14.0", "Aegis", "埃癸斯", "零硬编码 · 隐私护盾 · 开箱可部署"),
    ("13.0", "Pantheon", "万神殿", "多 Agent 联邦 · MoE 门控"),
    ("12.0", "Chronos", "克罗诺斯", "双时间轴有效期"),
    ("11.0", "Hyperion", "海伯利安", "线程本地连接池 · 性能纪元"),
    ("9.1", "Mnemosyne", "谟涅摩绪涅", "潮浪并忆 · 双策分档"),
)

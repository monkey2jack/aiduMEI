"""
ducky.version — aiduMEM 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v17.0 Themis (忒弥斯 · 正义女神 · 治理秩序纪元)
    核心主题: 将 Mímir 联邦记忆系统的三大治理理念融入 aiduMEM
    1. 变更事件账本 (fact_events): 借鉴 Mímir Event Ledger，每次冲突消解留可审计记录
    2. 敏感级别分档 (sensitivity): 借鉴 Mímir 三级安全模型，facts 表新增敏感标记字段
    3. 轻量候选结晶暂存: SkillCrystallizer 遵循"LLM 只能建议，人工才能 approve"铁律

    代码质量修复 (ConflictResolver/TreeMemory/SkillCrystallizer):
    - ConflictResolver: 快速路径优化（命中前不查 DB）+ 规则集脱敏可配置
    - TreeMemory: fact_count 改精确匹配，新增 get_ancestors 向上追溯，根节点可注入
    - SkillCrystallizer: 过滤噪声分类 + procedure 只记操作键不塞完整内容 + approve 接口
"""

SERVICE_VERSION = "17.0.0"
FULL_VERSION = f"v{SERVICE_VERSION}"
CODENAME = "Themis"
CODENAME_ZH = "忒弥斯"
DISPLAY_NAME = f"aiduMEM {FULL_VERSION} · {CODENAME_ZH}"

# 架构代号：治理秩序 · 事件账本 · 敏感分档 · 有序结晶
ARCHITECTURE = "Governed Self-Evolving Memory OS"

# 历史版本谱系（大版本代号，最新在前）
LINEAGE = (
    ("17.0", "Themis", "忒弥斯", "治理秩序 · 事件账本 · 敏感分档 · Mímir三借鉴"),
    ("16.0", "Opus Octopod", "opus八爪鱼", "冲突消解 · 树状记忆 · 技能结晶"),
    ("15.1", "Kalliope", "卡利俄佩", "代码瘦身 · FTS去重 · legacy精简"),
    ("15.0", "Iris", "伊里斯", "官方通道 · 惰性热载 · 静默归零"),
    ("14.0", "Aegis", "埃癸斯", "零硬编码 · 隐私护盾 · 开箱可部署"),
    ("13.0", "Pantheon", "万神殿", "多 Agent 联邦 · MoE 门控"),
    ("12.0", "Chronos", "克罗诺斯", "双时间轴有效期"),
    ("11.0", "Hyperion", "海伯利安", "线程本地连接池 · 性能纪元"),
    ("9.1", "Mnemosyne", "谟涅摩绪涅", "潮浪并忆 · 双策分档"),
)

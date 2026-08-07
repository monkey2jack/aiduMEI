"""
ducky.version — aiduMEM 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v18.1 Zeus (宙斯 · 检索自进化纪元)
    核心主题: SimpleMem 核心理念 EvolveMem 融合，建立闭环反馈
    1. EvolveMem 引擎: /evolve/feedback 与周期性 boost/decay 权重调整
    2. MCP 工具扩充: expose evolve_feedback 与 evolve_report
    3. 全方位质量审计: 清理架构遗留瑕疵，确保高稳定性
    4. 三大借鉴完全落地: MemPalace(原味抽屉) + code-review-graph(代码图谱) + SimpleMem(检索进化)

    竞品融合来源:
    - MemPalace (58k⭐): Verbatim Storage → Raw Drawer
    - code-review-graph (29k⭐): AST blast radius → Code Graph
    - SimpleMem (3.7k⭐): EvolveMem → 检索自进化 (Phase 3 闭环)
"""

SERVICE_VERSION = "18.1.0"
FULL_VERSION = f"v{SERVICE_VERSION}"
CODENAME = "Zeus"
CODENAME_ZH = "宙斯"
DISPLAY_NAME = f"aiduMEM {FULL_VERSION} · {CODENAME_ZH}"

# 架构代号：检索自进化 · 反馈闭环 · 核心重构
ARCHITECTURE = "Feedback-driven Evolving Memory OS"

# 历史版本谱系（大版本代号，最新在前）
LINEAGE = (
    ("18.1", "Zeus", "宙斯", "检索自进化 · EvolveMem 反馈闭环"),
    ("18.0", "Zeus", "宙斯", "原味抽屉 · 代码图谱 · 五大竞品精华融合"),
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


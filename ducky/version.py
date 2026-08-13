"""
ducky.version — aiduMEI 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v19.0 Athena (雅典娜 · 从记忆到智慧)
    核心主题: 认知闭环后半程——记完之后如何变聪明
    1. Reflect 主动反思 (P0-3): 定期/会话结束触发，提炼模式/矛盾/知识缺口为洞察
    2. 记忆去重自编辑 (P0-2): 写入前 LLM 判重复/冲突/全新，合并而非追加，可回滚
    3. 记忆类型分离 (P1-1): FACTS/PREFERENCES/EXPERIENCES/OBSERVATIONS/REFLECTIONS/DECISIONS 六型
    4. 自动 Skill 生长 + 精炼淘汰 (P1-2): 轨迹→草稿→人工approve；低效用技能自动待淘汰
    5. 记忆递归精炼 (P1-3): 多条碎记忆聚类压缩为高层抽象，可回滚
    6. 人格记忆基座 Persona Memory Layer: 一句话人设展开为可检索自传体记忆库，synthesis/grounded 双模式
    7. 双时间轴 + 时间感知检索 (P0-1/P0-4): valid_from/valid_to/recorded_at + 向量+BM25+时间衰减混合召回

v18.3 Zeus (宙斯 · 多模态感知纪元)
    核心主题: 无损升级机制 + 多模态视觉记忆 + Obsidian 双链联动
    1. 无损秒级平滑升级 SOP: user_version schema 版本化 + ALTER TABLE 增量补丁
    2. 多模态 API: /add 原生支持 image_url，后端自动调用 Vision 模型生成 caption
    3. Obsidian 双链: Wikilink 解析器 + 实体图谱节点打通 + /api/obsidian/sync
    4. 前端适配: PULSE 统计多模态数据 / SETTINGS 展示 Vision 模型 / VAULT 渲染缩略图

v18.2 Zeus (宙斯 · 可视化洞察纪元)
    核心主题: aiduMEI 品牌升级 + 自带可视化控制台 + 全量代码审计
    1. aiduMEI 控制台: 六面板(PULSE/VAULT/MAP/RECALL/EVOLVE/SETTINGS)零依赖纯静态
    2. 后端自带 /ui 静态托管 + /api 别名层 + /config 路由(api_key 脱敏)
    3. 全量代码审计: recall_funnel NoneType 崩溃 / _load_patterns 逻辑 bug / 7处静默异常补日志
    4. 品牌升级: aiduMEM → aiduMEI (aidu Memory Engine Insight / 爱嘟优忆思)

v18.1 Zeus (宙斯 · 检索自进化纪元)
    核心主题: SimpleMem 核心理念 EvolveMem 融合，建立闭环反馈
    1. EvolveMem 引擎: /evolve/feedback 与周期性 boost/decay 权重调整
    2. MCP 工具扩充: expose evolve_feedback 与 evolve_report
    3. 全方位质量审计: 清理架构遗留瑕疵，确保高稳定性
    4. 三大借鉴完全落地: MemPalace(原味抽屉) + code-review-graph(代码图谱) + SimpleMem(检索进化)
"""

SERVICE_VERSION = "19.0.0"
FULL_VERSION = f"v{SERVICE_VERSION}"
CODENAME = "Athena"
CODENAME_ZH = "雅典娜"
DISPLAY_NAME = f"aiduMEI {FULL_VERSION} · {CODENAME_ZH}"

# 架构代号：从记忆到智慧——主动反思 · 自编辑 · 递归精炼 · Skill 自生长 · 人格记忆基座
ARCHITECTURE = "AI Thought Engine with Active Reflection, Self-Editing Memory, Recursive Refinement & Persona Memory Layer"

# 历史版本谱系（大版本代号，最新在前）
LINEAGE = (
    ("19.0", "Athena", "雅典娜", "从记忆到智慧 · 主动反思 · 记忆自编辑 · 递归精炼 · Skill生长 · 人格记忆基座"),
    ("18.3", "Zeus", "宙斯", "多模态感知 · 无损秒级升级 · Obsidian 双链联动"),
    ("18.2", "Zeus", "宙斯", "可视化洞察 · aiduMEI 控制台 · 品牌升级 · 全量审计"),
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


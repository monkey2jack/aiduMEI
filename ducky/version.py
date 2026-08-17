"""
ducky.version — aiduMEI 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v19.3.3 (审计回归修复与发布链接续版 · 2026-08-17)
    核心主题: 小猴审计修复 · 测试断言对齐 · 发布链接续
    1. 修复 persona_memory.py 嵌套 except-as-e 同名遮蔽导致的 NameError 回归（v19.3.1 静默异常治理时引入）
    2. 测试断言对齐: test_v19_3_hardening / test_v19_2 版本白名单同步，恢复测试套件全绿
    3. LINEAGE 谱系补全 19.3.2 / 19.3.3 条目
    4. 版本号五文件全量对齐 19.3.3，PyPI 发布链接续

v19.3.2 (legacy 路由 import 修复版 · 2026-08-17)
    核心主题: legacy_routes 缺失 import 补全 · /facts/add 接口 500 根治
    1. legacy_routes.py 补全 9 个缺失 import（re / datetime(_dt) + 7 个 legacy_helpers 函数），
       修复服务能启动但 /facts/add 一写入即 NameError 500 的隐藏 bug
    2. 版本号五文件全量对齐 19.3.2

v19.3.1 (审计修复与发布链对齐版 · 2026-08-16)
    核心主题: 审计问题修复 · 版本号全量对齐 · 静默异常可观测 · 占位符根除
    1. 静默异常治理: 18 处 except Exception: pass 补 debug/warning 日志上下文，safe-ignore 处补注释
    2. Reranker 占位符根除: 配置兜底默认值从 your-rerank-endpoint 改为空串，缺配置时干净跳过不再发 DNS 请求
    3. 脚本层 HTTP timeout 补齐: restore_bg.py 补 timeout=15
    4. 版本号五文件全量对齐: version.py / pyproject.toml / manifest.json / __init__.py / CHANGELOG.md
"""
from __future__ import annotations

SERVICE_VERSION = "19.3.3"
FULL_VERSION = f"v{SERVICE_VERSION}"
CODENAME = "Athena"
CODENAME_ZH = "雅典娜"
DISPLAY_NAME = f"aiduMEI {FULL_VERSION} · {CODENAME_ZH}"

# 架构定位
ARCHITECTURE = "Production-Grade AI Wisdom & Long-Term Memory Engine with 3-Layer Injection Defense, Multi-Store Consistency & Unified Scoring"

# 历史版本谱系（最新在前）
LINEAGE = (
    ("19.3.3", "Athena", "雅典娜", "审计回归修复 · 测试断言对齐 · 发布链接续"),
    ("19.3.2", "Athena", "雅典娜", "legacy 路由 import 修复 · /facts/add 500 根治"),
    ("19.3.1", "Athena", "雅典娜", "审计修复 · 静默异常可观测 · 占位符根除 · 版本号全量对齐"),
    ("19.3.0", "Athena", "雅典娜", "架构大一统 · 召回打分单一真相源 · 单例加锁治理 · 模块解耦与防线统一"),
    ("19.2.1", "Athena", "雅典娜", "生产热修复 · 助手深度复验"),
    ("19.2.0", "Athena", "雅典娜", "安全筑基 · 一致闭环 · 观测透明 · 检索提质 · 架构收敛 · 实事求是"),
    ("19.1.2", "Athena", "雅典娜", "审计补丁自审修复 · MCP 鉴权兼容 · 六型回填生效"),
    ("19.1.1", "Athena", "雅典娜", "审计补丁 · 接口安全 · MCP 契约 · 版本号诚信"),
    ("19.1", "Athena", "雅典娜", "审计修复 · 联邦隔离 · 主链接线 · 卖点诚信"),
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

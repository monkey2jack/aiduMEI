"""
ducky.version — aiduMEI 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

本文件只保留：版本号常量、当前版本块、谱系表。逐版本的详细整改叙事
（含旧名 aiduMEM 时代的全部历史条目）统一收录于 CHANGELOG.md ——
v20.4.1a 起不再双写（四方外审 Sonnet #4：version.py 曾长达 1693 行，
实际变成第二份变更日志，与 CHANGELOG 互为腐化源）。

v20.4.1 (正式版 · 四方网页外审 + 用户审计整改收口 · 2026-09-09)
    主题：**防线接入链路，复杂度开始回吐。**
    四方网页版外审（GPT Luna / Sonnet 5 / Grok / Gemini 3.8 Flash）经逐条
    file:line 自查：采纳 12 条，驳回 5 条（全部 Gemini 虚构/误判），降级 1 条。
    1. A1 CI 自动触发（Sonnet P0 + Luna P1 双证）：test.yml 恢复
       pull_request 全量 + push→main 精简（pytest job 跳过，PR 已把关、
       直推有本地 push_gate），保留 dispatch/call；触发面守卫同步改判据。
    2. A2 版本口径收口（Luna）：README/README_EN 残留「保持 v20.3」改
       历史时态；新增口径守卫——现在时加粗版本宣称必须等于 SERVICE_VERSION。
    3. B1 ruff 扩门禁（Sonnet）：F401×176 + F541×12 清零，F401/F541 入 select。
    4. B2 圈复杂度（Sonnet，radon 复核全中）：cascade_delete_memory(66)/
       cascade_delete_all(53)/score_and_rank_candidates(61) 拆分至 <15。
    5. B3 version.py 瘦身（Sonnet）：1693 → ≤100 行，叙事归 CHANGELOG。
    6. B4 bandit 噪音清零（Sonnet）：9 处 md5/sha1 补 usedforsecurity=False。
    7. C 面：mcp_server urllib→httpx；ARCHITECTURE(v14) 归档 docs/；
       新增 CONTRIBUTING.md / SECURITY.md；依赖单源评估；CJK BM25 时间表。
    8. 不采纳：Gemini 五条（GHCR 流水线/无注入清洗/幂等不足/无异步队列等）
       全部经 file:line 复核驳回，机制均在仓；Luna 单进程系设计决策，
       状态外置转 v21+ 路线图。
    9. 用例总数 1835 → 1837（--collect-only）。四环 2026-09-09 本树实测：
       开发机 1825+12 / 干净 venv 1812+25 / 生产沙箱 1826+11 / 全轴 1835+1。
    10. b 阶段收口（用户审计 2🔴 根因「整改未部署」）：部署生产后清零
       （冒烟 PASS、readyz 全 true、schema 2→3 平滑）；data_dir_writable
       匿名可见裁决保留（理由入 docs/HEALTH.md）；异步一致性窗口与冷启动
       语义入双语 README 与 AGENTS.md；评审申请须标被审代码位置入 SOP。

v20.4.0 (正式版 · 三方审计 P0/P1 整改 · 断点续修四环复测收口 · 2026-09-09)
    详录见 CHANGELOG.md「## v20.4.0」段。
"""
from __future__ import annotations

SERVICE_VERSION = "20.4.1"
FULL_VERSION = f"v{SERVICE_VERSION}"
# v20 deliberately has no current mythological codename.  Keep the symbols as
# ``None`` for old integrations that import them, but all public/runtime
# contracts use the two-part version and DISPLAY_NAME instead.
CODENAME = None
CODENAME_ZH = None
DISPLAY_NAME = f"aiduMEI {FULL_VERSION}"

# 架构定位
ARCHITECTURE = "Production-Grade AI Wisdom & Long-Term Memory Engine with 3-Layer Injection Defense, Multi-Store Consistency & Unified Scoring"

# 历史版本谱系（最新在前）
LINEAGE = (
    ("20.4.1", "", "v20.4.1", "正式版 · 四方网页外审+用户审计整改 · CI接入链路/复杂度回吐/版本源单源化 · 2026-09-09"),
    ("20.4.0", "", "v20.4.0-alpha", "alpha 阶段快照 · 六方外审整改 · 对外声称与外界对账 · 开工 2026-09-08"),
    ("20.3", "", "v20.3.2", "正式版 · 五方外审整改 · 一致性与底层 · 2026-09-03（pre 09-01 · beta 09-02）"),
    ("20.3.1", "", "v20.3.1", "九份审计整改 · 仪器读世界 · 2026-09-01"),
    ("20.3.0", "", "", "优忆思 · Agent 入口与可操作性 · 生效自证"),
    ("20.2.5", "", "", "两份审计整改 · F-03 假修复真修 · 删除三态 · Ruff 进门禁"),
    ("20.2.4", "", "", "差异化时效衰减 · 纠正语只登记不判决 · 收益面如实标注"),
    ("20.2.3", "", "", "外部审计整改 · 入门依赖补齐/配置雷全仓拆除/登录爆破护栏"),
    ("20.2.2", "", "", "LLM 蒸馏腿挡位化 · 传输层盲重试掐除 · 断供写入确定性直写秒回"),
    ("20.2.1", "", "", "自动挡外审整改 · 拆配置雷/启动重放兜底/verbatim 单删闭合/重放防自我复制"),
    ("20.2.0", "", "", "智慧引擎自动挡 · 双引擎/熔断切换/挡位诚实 · 断供演练实机验证后公开"),
    ("20.1.1", "", "", "公开后外审加固 · 限流护栏/metadata 白名单/R-18 删除链/守卫三连"),
    ("20.1.0", "", "", "确定性兜底与诚实召回 · 五份外审 R-01~R-17 闭合后公开"),
    ("20.0.1", "", "", "mem0ai 2.0.19 兼容 · 删除链孤儿清理 · 私有预发布"),
    ("20.0", "", "", "全量记忆域隔离 · 可复现评测 · 后端契约与数据生命线"),
    ("19.5.0", "Athena", "雅典娜", "脱敏闸门 · 七面扫描器焊入发布链 · 空词表拒绝运行 · 负向对照可证伪"),
    ("19.4.3", "Athena", "雅典娜", "发布卫生 · 发行包也是公开面 · 与 v19.4.2 行为等价"),
    ("19.4.2", "Athena", "雅典娜", "守卫扩面 · 集成件凭据贯通 · 元测试锁死守卫射程 · 崩溃循环可见"),
    ("19.4.1", "Athena", "雅典娜", "审计补丁 · 鉴权贯通与租户闭环 · 静默失败终结 · 删除权兑现 · 宣称即承诺"),
    ("19.4.0", "Athena", "雅典娜", "明镜工程 Phase 1 · 原文保真层 · 生产审计修复 · 注入框架服务端自防御 · LLM 通道根治 · 治理账本无死角"),
    ("19.3.3", "Athena", "雅典娜", "审计回归修复 · 测试断言对齐 · 发布链接续"),
    ("19.3.2", "Athena", "雅典娜", "legacy 路由 import 修复 · /facts/add 500 根治"),
    ("19.3.1", "Athena", "雅典娜", "审计修复 · 静默异常可观测 · 占位符根除 · 版本号全量对齐"),
    ("19.3.0", "Athena", "雅典娜", "架构大一统 · 召回打分单一真相源 · 单例加锁治理 · 模块解耦与防线统一"),
    ("19.2.1", "Athena", "雅典娜", "生产热修复 · 深度复验"),
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

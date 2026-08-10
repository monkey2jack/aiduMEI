"""
ducky.version — aiduMEI 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v18.3 Zeus (宙斯 · 登录门禁纪元)
    核心主题: 控制台登录页 + 密码门禁
    1. /ui/login.html: 与主页同款背景/header/footer 的简单密码入口
    2. /api/login 端点: AIDUMEM_UI_PASSWORD 环境变量校验（不入仓库）
    3. index 登录态拦截: 未登录访问自动跳转 login.html

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

SERVICE_VERSION = "18.3.0"
FULL_VERSION = f"v{SERVICE_VERSION}"
CODENAME = "Zeus"
CODENAME_ZH = "宙斯"
DISPLAY_NAME = f"aiduMEI {FULL_VERSION} · {CODENAME_ZH}"

# 架构代号：可视化洞察 · 自带控制台 · 品牌升级
ARCHITECTURE = "Visual Insight Memory Engine with Built-in Console"

# 历史版本谱系（大版本代号，最新在前）
LINEAGE = (
    ("18.3", "Zeus", "宙斯", "登录门禁 · 控制台登录页 · 密码校验"),
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


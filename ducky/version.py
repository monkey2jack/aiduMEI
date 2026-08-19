"""
ducky.version — aiduMEI 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v19.4.2 (守卫扩面 · 集成件凭据贯通 · 2026-08-19)
    核心主题: 守卫的射程必须覆盖缺陷的分布 · 带了头不等于带了钥匙
    定性: **v19.4.1 的收口版**，不引入新功能。
    背景: v19.4.1 上线后的生产复审（含 Hermes Agent 升级）发现，门禁本身修对了，
    但「谁需要带钥匙」这份名单列漏了。v19.4.1 写了守卫测试防调用方漏带凭据，
    而那条守卫只扫 scripts/ 一个目录 —— 缺陷却分布在仓库根、integrations/、
    mcp_server.py 上，一个都没被扫到。
    **守卫的射程小于缺陷的分布，比没有守卫更危险**：它提供「已经防住了」的错觉。
    方法: 本版核心动作不是「再修几个文件」，而是用一条**元测试把守卫自己的射程焊死**
    —— 断言守卫覆盖集合 ⊇ 全仓实际发起 HTTP 请求的文件集合。这条元测试首次运行
    当场揪出两个未数到的入口点，扩面后二次运行又揪出一个。计划点名 5 个，实际 9 个。

    —— 🔴 凭据贯通（门禁开启后会静默 401 的调用方）——
    1. integrations/aidumem-inject.sh: 补 Bearer 与 .env 兜底链（AIDUMEM_ENV_FILE →
       $AIDUMEM_HOME/.env → ~/.aidumem/.env → ./.env），401/403 单列诊断，
       新增 --selftest（不可达返回 4，且永不阻断 LLM 调用），去掉写死的 /root 绝对路径。
    2. mem0_sync.py / seed_demo.py / seed_facts.py: 统一改用 ducky.utils.api_auth_headers()，
       并补 sys.path（cron 的 cwd 不是仓库根）。
    3. mcp_server.py: 原自带 os.environ 快照，两个坑 —— 无 .env 兜底（门禁一开工具调用全 401）；
       import 期固化成模块常量（运行期轮换凭据不生效）。现复用同一真相源。
    4. integrations/cursor-hook/aidumem-on-save.sh（此前完全无凭据）: 补 AUTH_ARGS 与
       401/403 提示；数组展开用 ${ARR[@]+"${ARR[@]}"} 兼容 bash 3.2 + set -u。
    5. integrations/cursor-hook/claude-code-hook.py（此前完全无凭据）: 优先复用 ducky.utils，
       被拷出仓库时回落内置同款兜底链；401/403 附排查提示。
    6. integrations/hermes-plugin/aidumem/__init__.py: v19.4.1 已写 Authorization 头，
       但 token 只从环境变量读 —— gateway 拉起插件时环境近乎为空，**代码里明明带了
       Bearer，实际每次请求都是空 token**。补兜底链；401/403 从 debug 提到 warning。
    7. ducky/utils.py: load_env_file() 兼容 `export KEY=VALUE`。部署的 .env 常给 shell
       source 用自带 export 前缀 —— 此前 bash 侧认、Python 侧不认，同一份文件两种结果，
       症状与「压根没配 token」一模一样，排查极易走偏。

    —— 🛡️ 守卫扩面（本版真正主题）——
    8. 扫描范围从 scripts/ 扩到 scripts/ + 仓库根 *.py + integrations/**（含子目录）。
       api_server.py 显式排除 —— 它是门禁的实施者，不是通过门禁的人。
    9. 新增 tests/test_v19_4_2_auth_coverage.py 元测试: 断言守卫覆盖集合 ⊇ 全仓 HTTP
       调用方集合，改窄射程立刻红灯。
    10. 独立集成件（integrations/ 下、会被拷进宿主配置目录、无法 import ducky）允许自带
        凭据实现，但必须实现同一条兜底链 —— 只带 Authorization 头不算修好。

    —— 🟠 静默失败可观测 ——
    11. ducky/mem0_runtime.py: 历史 user_id 映射首次调用自报状态。脱敏把映射规则整个交给
        环境变量，而「没配」与「配好了」行为上一模一样，区别只在某天有人问
        「我那批老记忆怎么搜不到了」。
    12. deploy/aidumem-sync.service: 补 StartLimitIntervalSec / StartLimitBurst。没有它，
        崩溃循环一直停在 activating 而永不进 failed，按 failed 告警的监控等不到那一刻。
    13. deploy/logrotate/aidumem: 用 copytruncate —— 单元是 StandardOutput=append:，
        改名切割后进程仍写旧 inode，日志凭空消失。
    14. pyproject.toml / requirements.txt: 补同步守护进程依赖声明（此前靠部署机恰好装过）。

    —— 🟢 品牌与版本 ——
    15. 前端品牌残留清理（标题 / description / alt / 字标 / 错误文案 / 图谱中心节点 / 注释）。
        字标是标签拆分写法 aidu<b>MEI</b>，全局 sed 扫不到 —— v19.4.1 的改名正是从这里漏出去的。
    16. /docs 的 FastAPI 标题改为 aiduMEI API。logger 名、/health 的 service 字段、
        各模块 docstring 里的 aiduMEM 一律不动 —— 机器契约与历史内部名，生产监控按其匹配。
        环境变量前缀 AIDUMEM_* 同理保持不变。

v19.4.1 (审计补丁 · 鉴权贯通与租户闭环 · 2026-08-18)
    核心主题: 宣称即承诺 · 静默失败终结 · 删除权兑现 · 一道门禁两把钥匙
    定性: **审计补丁版**，不引入新功能。修的全是「文档说了但代码没做到」的裂缝。
    方法: 审计从「逐行读代码」改为**探针实测** —— 对 README/CHANGELOG 每一句宣称，
    写最小可运行程序去试着推翻它。四条宣称被实测推翻，逐条修复并写进断言。

    —— 🔴 安全与数据权利 ——
    1. P0-1 鉴权贯通「一道门禁两把钥匙」: 新增 ducky/security/auth.py。
       修复前两种部署都不可用——只设 UI 口令则接口 200 全裸奔（口令仅前端标记）；
       只设 API 令牌则控制台登录后全 401 报废（前端从不发 Authorization）。
       根因是认证结果没有服务端载体。现 /login 签发 HttpOnly+SameSite=Lax
       session cookie，与 Bearer 令牌任一有效即放行；新增 /logout 服务端撤销。
       存量零破坏: 口令哈希加 source=auto|user 标记，自动生成的口令只守控制台登录，
       不改变既有回环调用方（插件/MCP/cron）的 API 语义。
    2. P0-2 facts 层租户可见性贯通: 新增 tenant_clause()，覆盖 9 个路由与注入出口；
       宽松档（默认，兜住未标记归属的历史数据）/ 严格档（AIDUMEM_STRICT_TENANT=1）双档。
    3. P0-2b 跨租户静默覆盖（施工中新发现，比泄漏更严重）: /facts/add 原将 agent_id
       恒写常量，而唯一约束是 (agent_id, category, fact_key) —— 不同租户写同一键位
       会命中同一约束，后写者直接销毁前者的值。现按租户落 agent_id。
    4. P0-3 移除无 WHERE 全表删: 各仓原有 `if user_id == "default": DELETE FROM 表`，
       而 default 正是系统默认租户 —— 清 default 会连带清空所有租户。
       现一律精确 WHERE user_id=?；全库清空抽成显式 confirm=True 入口。
    5. P0-4 删除权兑现到原文层: cascade_delete_memory 原清 5 个库独漏 verbatim_turns，
       含敏感信息的原文删除后仍可检索。补第 6 步按内容哈希精确清理双侧。
    6. P0-4b 原文条目可删（实机发现，P0-4 只修了一半）: /search 返回 verbatim:<n>
       句柄是调用方唯一句柄，但 /delete 不认它 —— 返回成功却什么都没删。此类原文
       常无对应 mem0 记忆，遂成「可检索但删不掉的孤儿」。新增按句柄精确删除，
       强制租户匹配防越权，删前留 tombstone。

    —— 🟠 功能真伪与可观测 ——
    7. P1-1 幂等键根治: 判重键原含 recorded_at，而生产载荷（纯字符串）无时间戳，
       回落 now() 导致永不撞键，实测同一轮重放 3 次落 3 条。改为稳定因子
       (user_id, content_hash, session_id)；重复表述累加 occurrences 而非堆行。
    8. P1-2 中文切词与 trigram 索引对齐: 原切 2-gram 而索引为 trigram，**中文查询
       恒不命中索引**，一直全表扫描（20 万条实测稀有词 32.8ms）。改 3-gram 后
       同量级 0.05ms；两处重复切词实现收敛为一份；新增 fts_is_authoritative()
       避免权威零命中后白扫 LIKE；召回结果带 _recall_path(fts|like) 自证路径。
    9. P1-3 observations 幂等建表: 该表自 v7 起只有读取方、全仓无 DDL，全新部署
       /observe 直接 500。列集对齐生产存量 schema，user_id 用 ADD COLUMN 幂等补齐，
       读取路径先探测列集再决定是否施加过滤（迁移可能失败，读取不能依赖它成功）。
    10. P1-4 4xx 不再被降级成 500: 注入拦截的 400 被外层 except Exception 吞掉再包
        500，调用方无法区分「内容被拒」与「服务端故障」，带重试的客户端会死循环。
        18 处统一先放行 HTTPException，配 AST 源码守卫防复发。

    —— 🔍 三个「静默失败自我掩盖」连环案（实机排查所得）——
    11. 兼容门面缺口致 consolidator 静默死亡三周: v11.1 重构把显著性能力拆进子包，
        门面只转发两个钩子，而 consolidator 仍按老接口导入 6 个符号，自 2026-07-26
        起每日凌晨崩在 import 行、日志累积 18 次同样堆栈。期间衰减/指标/冲突检测/
        技能结晶/教训闭环全部未运行，而 /health 一直全绿（这些活儿不在服务进程里）。
        修法是补门面而非改调用方，保持向后兼容。同时补 ducky.utils.CONSOLIDATOR_LOCK。
    12. salience/evolve 级联清理从引入起从未执行: wal_engine 用的表名与列名双错
        （memory_salience 真名 salience 且无 user_id 列；evolve_snapshots 表不存在），
        错误被 except 吞成 debug，计数恒报 0。后果是一条自我掩盖链——salience 留下
        252 条幽灵 id → 被当正常记忆持续衰减 → 进入淘汰名单 → consolidator 逐个删
        「早已不存在的东西」→ 日志漂亮报「成功删除 25/25」而向量库分毫未变。
        新增 delete_salience / prune_orphan_salience / delete_evolve_by_memory_ids。
    13. SkillCrystallizer SQL 方言错误: GROUP_CONCAT(DISTINCT x, sep) 在 SQLite 报错，
        异常被吞后输出「技能结晶感知完成: 生成 0 个候选项」—— 看似「暂时没发现模式」，
        实则该 SQL 从未成功执行。DISTINCT 移进子查询后实测正常产出候选项。

    —— 🛡️ 备份纪律与 cron 凭据 ——
    14. backup_gate 一致性快照: 原流程「先算校验和 → 再逐个打开库跑完整性检查」，
        而打开 WAL 库会重建 -shm 并合并日志，当场打废刚算好的基线 —— create 报通过、
        require 立刻拒绝，硬门禁 100% 拦人，备份纪律退化为形同虚设。改用 SQLite
        在线备份接口生成已合并日志的单文件快照，不留伴生文件，校验和最后算。
        **不变量: 校验动作本身不得破坏校验基线。**
    15. cron 凭据兜底: 服务靠 systemd EnvironmentFile 读令牌，但 cron 不加载 .env
        （实测取到 None）—— 门禁一开，定时任务下次触发即集体 401，且失败只写日志
        无人知晓。新增 ducky.utils.load_env_file / api_auth_headers 作为凭据单一
        真相源，9 个运维脚本统一复用，health_check 补 sys.path（cron 的 cwd 非仓库根）。

    —— 🟡 供应链与加固 ——
    16. pyproject 依赖下限对齐 requirements 实锁 + requires-python >=3.10
        （此前 pip 安装与源码安装跑两套依赖树）
    17. 口令改 PBKDF2-HMAC-SHA256 200k 轮，文件权限 0600，旧单轮 sha256 首次登录
        自动升级；改密撤销全部会话；口令下限 4→8 位
    18. echarts 落本地 frontend/js/vendor（去掉无 SRI 的第三方 CDN 外链，离线可用）
    19. router_usage（ssh + exec 形态）默认禁用，需显式 AIDUMEM_ROUTER_USAGE_ENABLED=1
    20. /docs /redoc /openapi.json 纳入门禁（135 个端点清单等于攻击面地图），
        AIDUMEM_PUBLIC_DOCS=1 可显式放开；登录与健康检查永久免凭据
    21. /stats 的 vision_count / obsidian_count 按租户收窄（原为全库计数，
        陌生租户可从中推断本机记忆总规模，属量级侧信道泄漏）
    22. 严格档下 /events/history 与 /opinions 补租户校验（自增整数 id 可枚举）

    —— 🟢 文档诚信（宣称即承诺铁律的执行）——
    23. 「租户硬隔离」改为准确的「按租户收窄可见性」并明示单机自托管定位；
        README_EN 补齐 Testing & Quality 与 Security Model 两章并与中文版对齐
    24. 测试数字改为**自校验**: 新增守卫从 pytest --collect-only 取真值与 README
        比对，并校验「通过数 + 跳过数 = 总数」—— 数字过期会立刻红灯，而非靠人手同步
    25. 补充 trigram 中文切词策略与 LIKE 兜底边界；删除范围清单补上原文层

    质量: 339 通过 / 12 跳过（完整环境 351 全绿）· 编译 0 错误 · 脱密 0 泄漏
    新增测试 107 项，全部遵循「反假绿灯纪律」：载荷/凭据/查询形态多形态并测，
    索引类断言校验 _recall_path 而非仅看命中数。

v19.4.0 (明镜工程 Phase 1 · 原文保真层 · 生产审计修复版 · 2026-08-17)
    核心主题: 说过的话一字不丢 · 原文证据与原子事实融合召回 · 生产路径自防御 · 治理账本无死角
    背景: AML 榜单调研证实显式事实召回靠「原文保真 + 混合检索」，不靠更花的抽取。
    我们不参赛，只把干货拿来打磨，开源惠及大众。
    对 v19.4.0 生产部署全面审计（2🔴5🟡）后逐项修复，随 v19.4.0 一并发布。

    —— 明镜工程 Phase 1 · 原文保真层 ——
    1. 新增 ducky/verbatim_vault.py 原文保真层: verbatim_turns 表（facts.db，租户硬隔离 +
       幂等去重）+ verbatim_fts trigram 全文索引（text_fts.db），mem0 抽取之外的第二层
    2. /add 注入防御通过后逐字原文落库；/search 原文证据融合返回（主干优先 + 配额保留）
    3. cascade_delete_all 级联清理原文层，绝不留孤儿；启动时幂等建表
    4. 失败干净降级，绝不阻断主链路；对现有 facts 零影响

    —— 生产审计修复（2🔴5🟡 逐项）——
    5. 🔴-A B4 注入框架服务端出口包装: /facts/inject-context 返回即带框架 +
       <memory> 标记，hook 侧凭标记防双重包装，生产路径不依赖 hook 也自防御
    6. 🔴-B call_llm 根治上游网关 SSE 假响应: 请求显式 stream:False +
       _parse_completion_body 三态兜底解析（标准 JSON / 拼接体 / 真 SSE 流）；
       生产实测补强——上游推理模型，思考与输出共享预算，
       检测到「推理截断」（content 空 + finish_reason=length + 有 reasoning_content）
       自动放大预算 ×4 重试（封顶 4096），治理评估器恢复真实运转
    7. 🟡-A 噪声规则升级: 键盘行/重复字符/连续数字/纯符号随机组合识别，
       含 CJK 一律放行交 LLM，不误杀真实记忆
    8. 🟡-B backup_gate 嵌进 pre-upgrade-check 硬门禁: 备份→require 校验→
       冒烟→cron→e2e 五步，无验证备份拒绝升级
    9. 🟡-C 账本 target_id 别名展开: fact:{key}/fact:{id}/裸 id 一个参数查全链
    10. 🟡-D 次路径补账本与治理: 联邦 insert 全治理 + 三路径账本，
        refine_memory/ai-self 内部路径补账本
    11. 🟡-E 既有备份补 SHA256SUMS（部署时执行）

v19.3.3 (审计回归修复与发布链接续版 · 2026-08-17)
    核心主题: 审计修复 · 测试断言对齐 · 发布链接续
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

SERVICE_VERSION = "19.4.2"
FULL_VERSION = f"v{SERVICE_VERSION}"
CODENAME = "Athena"
CODENAME_ZH = "雅典娜"
DISPLAY_NAME = f"aiduMEI {FULL_VERSION} · {CODENAME_ZH}"

# 架构定位
ARCHITECTURE = "Production-Grade AI Wisdom & Long-Term Memory Engine with 3-Layer Injection Defense, Multi-Store Consistency & Unified Scoring"

# 历史版本谱系（最新在前）
LINEAGE = (
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

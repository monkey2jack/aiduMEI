"""
ducky.version — aiduMEI 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

本文件只保留：版本号常量、当前版本块、谱系表。逐版本的详细整改叙事
（含旧名 aiduMEM 时代的全部历史条目）统一收录于 CHANGELOG.md ——
v20.4.1a 起不再双写（四方外审 Sonnet #4：version.py 曾长达 1693 行，
实际变成第二份变更日志，与 CHANGELOG 互为腐化源）。

v0.1.0 (对外 f0.1 · f 世代首版 · LoCoMo 跑分整改 · 2026-09-23)
    主题：**让原文库记住「事情什么时候发生」，而不是「什么时候存进来」。**
    1. 版本体系换代：对外统一 f*.*（f = future / fantasy / forever）。
       f0.1 非 PEP 440 合法值，故包版本另用 0.1.0，二者由 LINEAGE
       第三列钉死，不许各走各的。
    2. 🔴P0 根因（一）：_iter_turns 只读 message 自带的 timestamp，而真实调用方
       （含 /add 生产链路）把事件时间放在 metadata.recorded_at。
    3. 🔴P0 根因（二）：于是 ts=None → _normalize_ts 无条件回落 now()，把事件
       时间悄悄换成入库时间，且不报错不告警——典型假绿灯。2026-09-22 LoCoMo
       实测：时序题 39% 的证据带着入库时间进了答题上下文。表结构本就分
       recorded_at/created_at，故零 schema 迁移。
    4. 写线：_normalize_ts 加 fallback；store_verbatim 用上一直收着却没用的
       metadata。优先级 逐条 message.timestamp > 批次 metadata.recorded_at > now()。
    5. 读线：build_context 补读**顶层** recorded_at（verbatim 条目无 metadata，
       此前被漏读）。时序题证据时间戳覆盖率 61.0%→100.0%。
    6. 读线·生产末端：integrations/aidumem-inject.sh 渲染召回块时带上日期。
       此前只发正文——库里存着时间、/search 也返回了，却在注入那一刻丢掉，
       模型一问「上次是什么时候」只能猜。存得对、搜得到，但没给模型看。
    7. 防回归：verbatim_search 返回补 created_at。recorded_at 改后承载调用方
       任意格式，extract_timestamp 的 fromisoformat 会失败并回落 0.0，时间衰减
       将静默失效；created_at 恒 ISO 且 key 顺序更靠前，故时间衰减零回归而
       上下文拿到真事件时间。负向对照已钉死。
    8. 部署面（用户审计实锤）：第 6 条一度只躺在仓库里没送到宿主。config.yaml
       实际加载的是遗留名 ~/.hermes/agent-hooks/mem0-inject.sh，集成文档却通篇
       只写 aidumem-inject.sh；钩子是拷贝不是软链，升级不重部署即静默失效。
       已按 config.yaml 声明的真实路径部署并核验 md5 三处一致，文档补「升级必
       重部署 + 先读 config.yaml 认路径」。教训：验仓库文件等于没验。
    9. 防复发（用户审计三处追问）：新增 scripts/check_hook_deployment.py，不认
       文件名只认 config.yaml 声明的路径，按事件认源（否则「改过名+旧内容」会
       被降级放过），零钩子报「没测到」不报「通过」，并已并入 health_check 让
       定时巡检自动带上；注入日期粒度可配 AIDUMEI_INJECT_DATE=day|minute|off；
       scoring 的时间戳优先级抽为具名常量 TIMESTAMP_KEY_PRIORITY 并由守卫钉死
       created_at 必须先于 recorded_at（调换会让时间衰减静默归零）。
    10. README 重写（文档面换代，零代码改动）：删掉头部那串旧版本号功能罗列
        （那是 CHANGELOG 的职责），只留「当前公开版本 f0.1」+ f 世代含义；
        测试表里的旧树标识中性化——保留日期与 commit SHA（可追溯锚点），去掉版本语义。
    11. README 新增「优忆思」释义：MEI = Memory + Engine + Insight，
        优＝优化配置双引擎全自动 / 忆＝记忆底座与记忆逻辑 / 思＝借模型思考力省上下文，
        每条都钉到仓库里真实在跑的代码，不是修辞。
    12. README 新增跑分章节：首次把 LoCoMo 试跑成绩放进门面，含与 LangMem/Zep/
        OpenAI 全上下文/Mem0 的同口径分维度对比表、复现锚点，以及「可以讲/必须
        承认/不能讲」三段解读。短板与强项并列，明确声明试跑非定稿、不宣称 SOTA。
    13. README 新增「下一版本预期」表：每行带状态标记，时序那行写明「根因已修、
        待复跑」——目标列一栏都不是成绩。
    14. docs/BENCHMARKING-POSTURE.md 整页重写：原文开头「本版没有跑分」已随首次
        试跑过期，过期的承诺不许继续挂着装门面。
    15. 新增冲突标记守卫：全仓扫入库文件，行首残留 <<<<<<< / >>>>>>> 即红。
        起因是真事故——一次 merge 后 README 留了两组标记（两侧内容相同，肉眼像
        正常段落）跟着提交推到远端，而门禁五关全绿：静态关只看 Python、测试关
        只比数字、脱密关只找敏感词，没有一关看得见它。守卫刻意不引入新跳过轴
        （无 git 时遍历部署树兜底），自带射程断言与负向对照。
    用例总数 2208 → 2237（+13 条 f0.1 守卫 + 16 条 f0.1+ 整改守卫：6 条事件时间回归 + 6 条部署一致性与注入粒度 + 1 条冲突标记，每条自带负向对照，全部红→绿）。
    诚实边界：上述覆盖率由存量数据复算，只证明「读得到」；存量时间**值**仍是
    入库时间，真值须重跑评测。本版不宣称任何新跑分成绩。

[f0.1+ 整改] Layer1 容量合并误删记忆 (2026-09-24)
    性质：P0 数据安全整改，**不升版本号、不打 Tag、不发 Release**，只推 commit。
    故不进上面 v0.1.0 的编号列表 —— 它记的是那次发布的内容，这次不是一次发布。

    auto_merge_similar 名为「合并相似记忆」却一次相似度计算都没有：只按
    metadata.source 分组，把同来源的几百条不同话题记忆当成同类，只留最新 1 条，
    其余真删（用户现场单次删 794~864 条），且直接调 mem0 原生 delete 绕过
    tombstone —— 既删错了又删得找不回来。
    根因不是「少看了 category」：按任何标签分组再删同组旧的，思路本身就毁数据。
    四处改动：source 降为粗分桶、真按内容相似度聚类；AIDUMEI_AUTO_MERGE 默认
    off（宁可库满也不静默删）；删除前留墓碑快照；AST 守卫钉死。
    生产向量库仅 20 条、永不触发、测不出此缺陷，故另造 820 条触发环境对照：
    修复前删 819 条（误删 803），修复后删 16 条（误删 0）。
    同类自查（扫全仓 140 个 .py，扫描器先做负向对照证明射程）找到一处同类：
    instinct_graduation 的 graduate_to_skill 按 category 把 ≤10 条原始记忆蒸馏成
    1 条技能后删原始，同样绕过 tombstone。风险低于容量合并（定时早已禁用、生产
    14 项定时任务中 graduate 相关 0 项、须主动调 API），但查代码时另挖出更重的
    一处：memory.add 的返回值一眼不看就往下删 —— mem0 抽取返空时静默丢弃不抛
    异常，会出现「技能没写成、原始记忆全删光」的净蒸发。
    已修：_add_succeeded 校验写入落库才允许删；删除走墓碑 + 级联；
    POST /graduate 的 dry_run 默认 False → True（破坏性端点默认值掰回安全侧，
    属行为变更）。「升格」按 category 聚类语义上成立，故不加内容相似度判据。
    本次新增 16 条守卫，每条自带负向对照（总数见上面 v0.1.0 段，全仓单一口径）。

v22.0.0 (雷霆审计整改 · 默认从严 · 2026-09-20)
    主题：**身份派生，越权默认拒。**
    11 份雷霆审计（10 外部模型 + 用户）合并后 12 条 P0 全实锤整改：
    1. 众神殿管理面鉴权（grant/revoke/deactivate 须本人或 admin，空 caller 403）。
    2. caller↔凭据绑定第三态（strict/permissive/off），消灭「新 token 未登记即裸奔」。
    3. 众神殿空 caller 收紧：bearer 必须声明身份，session/回环保留主人直连。
    4. 注入边界跨语言一致性（shell 读线前缀同源 + /add 落库前中和）+ NFKC 归一化。
    5. 逃逸门组合闸（INSECURE_PUBLIC∧TRUST_PROXY∧无凭据 → 拒绝启动）。
    6. 治理引擎多语言注入防御（英文高危词表 + CJK 占比乱码检测 + nonce 边界）。
    7. 依赖合一（pyproject 下限对齐 requirements）+ echarts sha256 清单。
    8. 哨兵补全：health 聚合键名盲区 / cron 哨兵 flag / testclient 显式信任 /
       push_gate 装 hook / 三态纪律 / CC 棘轮 / MCP error 三态 / routes_config admin /
       PBKDF2 600k / auto_memory 禁 fallback / 谱系完整性探针。
    9. 哨兵补全：health 聚合键名盲区 / cron 哨兵 flag / testclient 显式信任 /
       push_gate 装 hook / 三态纪律 / CC 棘轮。
    10. B 面收口：MCP error 三态 / routes_config admin / chunked 文档边界 /
        PBKDF2 600k / auto_memory 禁 fallback / 谱系完整性探针。
    11. 产品面 + 元修复：README 卖点证据状态标注 / CHANGELOG Scope Rulings 表 /
        双前缀冻结 / README 状态标签 / 鉴权面普查守卫 / 三态纪律规范 /
        CC 棘轮守卫 / 鉴权负向对照模板。
    用例总数 2143 → 2208（+60，全部红→绿）。

v21.2.0 (Memmy 融改 · 检索层与轨迹学习一次到位 · 2026-09-16)
    主题：**记忆不再自己回声，也不再让同一件事占满名额。**
    调研 MemTensor/memmy-agent（MIT · 1.9k⭐）后只取设计不搬代码，六项一次落地：
    1. M2 回声抑制：sidecar 补溯源三列（schema v9），检索排除「本会话自己刚写入」的记忆。
    2. M4 MMR 多样性：最终截断走 λ·relevance − (1−λ)·redundancy，近义簇不再霸占名额；
       点火条豁免的是冗余惩罚而非排序本身（实现期自查纠正的一处语义错误）。
    3. M6 错误签名通道：pattern_extract 新增第八类硬事实 errsig + 检索侧有界加权。
    4. M1 轨迹级奖励信用分配：episode 两表 + w_i = λ·(1/n) + (1−λ)·归一化(γ^(n−i))，
       任务反馈按轨迹位置回传；**credit 维度默认权重 0** —— 装上不生效，等本仓
       自己的 /evolve/report 数据说话再开（上游参数不盲信）。
    5. M7 episode rollup（默认关）· M8 借阅留痕进事件账本 + 档案第八节「当前生效借阅」。
       用例总数 2048 → 2208（+95 条验收与整改守卫，全部红→绿）。
    6. 审计整改轮（2026-09-17）：溯源打标改走显式 metadata（不再依赖 contextvar
       隐式通道）；补 episode_ok 与 epistemic_session_coverage（7 天窗口）两个探针；
       回声抑制降级升 warning；AGENTS.md 良性判据前置。根因判定：生产 sidecar
       session 全空是**调用方未透传**，非服务端跨线程丢失（三路实验实证）。
    7. 自查轮（2026-09-17）：复审六项翻出 9 处「默认关/旁路/观测缺失掩盖着的空转」——
       dossier 借阅读错键名（revoked_at vs revoked）且漏过期判据；rollup 的 limit
       从未被使用、去重只覆盖前 6 条；MMR 在 boost 前那一刀误杀点火条；workspace
       快路绕开回声抑制；MCP 不传 session；conversation_id 键名两侧不等；降级腿丢
       bank_id/session_id；verbatim 元数据回填静默失败；errsig 正则两份拷贝。
       全部修复并补守卫，生效证据（errsig_hits/credit_applied/echo_suppressed）进遥测。
    8. 范围外缺口收口（2026-09-17）：/search 与 /search_trace 补跨殿借阅校验
       （v21.1 只织入了 recall_chain/session_search/dossier，最主要那条读路径
       收了 caller_user_id 却不校验）；四处授权拒绝统一转 403 并收窄到 HallError
       ——此前被兜成 status:error，无权限与服务端故障混成一件事。空 caller 照旧
       放行，存量调用方零破坏。
    9. 写入活性探针（2026-09-17）：/health 新增 ingest_liveness_ok（读写比判据）、
       新增 scripts/check_ingest_wiring.py 自查、agent_integration_check 补 host-wiring
       判据。由来：一个部署只挂了注入钩子没挂写入钩子，指标全绿却在持续失忆 ——
       本仓所有探针都在看「库里的记忆好不好」，没有一个在看「该进来的进来了吗」。
       文档侧把「两条线」与各宿主挂点写进 AGENT_INTEGRATION.md / 正典 / 双语 README。
    10. 写线补齐（2026-09-17）：上一条只做了「查得出」，这一条补上「装得上」——
        此前本仓根本没有写入侧脚本，config.yaml.snippet 与 INTEGRATION_GUIDE.md
        也只教注册 pre_llm_call，那个失忆的部署是照着我们自己的文档装的。新增
       integrations/aidumem-ingest.sh（Hermes post_llm_call）
        与 integrations/cursor-hook/claude-code-stop-hook.py（Claude Code Stop），与读线共用同一条 .env
        凭据/身份链、必带溯源三件套、失败 exit 0 但 stderr 留痕、各带会吵的
        --selftest（真写一条再回读）。定时清单加 ingest_wiring 哨兵（8 → 9 项）。
        连带修两处既有真缺陷：scripts/health_check.py 每 5 分钟只看 HTTP 200 不读
        health_status/degraded（全仓降级探针对定时哨兵一律不可见，新探针的唯一
        自动消费者是瞎的）；scripts/report.py 装齐门槛写死 < 8，清单一加任务就把「少装
        了新哨兵」判成装齐 —— 两者都改为跟着真实清单现算。
    11. 用户审计整改第二轮（2026-09-17）：写线接上后审计出 5 条、自查又挖出
        2 条更深的。最要命的是检索埋点根本不在主路径上 —— log_search_quality
        只在 recall_funnel 里调用，主 /search 走 mem.search 不经过它，于是
        evolve_queries 里只有 e2e_smoke 每小时一次的巡检记录（生产实测近 24h
        的 24 条全是 aidumei-smoke-*），而写入活性探针正拿这张表当「有人在用」
        的证据 —— 读到的全是自己的心跳，只要一天没聊天就必报写线断了。
        埋点已接到主路径并带 session，判据改用 ingest_conv_reads_24h。
        其余六条：读线从不透传 session（M2 回声抑制在 shell hook 路上一直空转，
        已透传并给探针加第三态）；origin_turn 恒 0（宿主 turn_id 是字符串，
        int() 必然失败，改用会话内 user 轮数）；跳过某轮不留痕（解析段 stderr
        被自己 2>/dev/null 掉了）；check_ingest_wiring.py 样本不足时沉默通过
        （加 --require-judgment）；技术日志污染语义层（两个写线钩子加保守过滤）；
        覆盖率分母混着后台通路会误导读数（分子分母一并报出）。
        evolve_queries 补 origin_session_id，已进迁移总账。
    12. 会话精华萃取（2026-09-17，用户提议落地）：第三条线 —— Hermes
        session_end → integrations/aidumem-distill.sh，会话结束时把「这一程最
        值得记住的事」提炼成一两句单独存一条。每轮写入存的是事实，存不下
        「这一程是怎么回事」。新增 ducky/session_distill.py 与 /session/distill
        端点（只提炼不落库，落库由钩子走 /add —— 精华必须进向量库才召回得到，
        且纯提炼端点可安全重跑）。独立慢衰减泳道 distill(0.3)，故意不复用
        emotion(1.5 快衰减)；情感权重取自既有情绪词表命中数，有界加成
        0.60~0.85，不是新造的分数；LLM 不可用退确定性降级并标 fallback。
        探针 distill_liveness_ok 盯第三条线（有会话却零精华＝没挂）。
        README 双语讲清「自动」自动在哪：三条钩子在说话前/说话后/聊完三个
        时机自己触发，接上之后不需要再对记忆做任何事。
    13. 宿主 mem0 内置插件 provenance 补丁归档（2026-09-18）：补齐宿主侧
        session/turn 三件套透传物料；服务端版本不变。
    14. PR #15 思路吸收与安全重做（2026-09-19）：按真实 `(user_id, bank_id)`
        域契约独立重做 active 域目录、当前域 Markdown dossier 导出与 profile
        联邦展示分组；移除伪 `all` 域、静态 demo 身份、硬编码 caller 与 CSP
        inline style。版本与 Release 不变，仅推 commit。
    落位纠正两处（指导书按公开认知写，实测生产代码后修正）：回声/MMR 落在
    scoring 单一真源而非仅 recall_funnel（主 /search 走 RecallEngine，两路都经打分出口）；
    episode 表进 evolve 库的 ensure_evolve_schema 而非 facts.db 迁移流。

v21.1.1 (文档补丁 · 内存挡位选择指导 + 冷备 v21.2 roadmap · 2026-09-15)
    主题：**让部署 Agent 看懂内存挡位取舍。** 无代码功能变化，测试基线不变。
    1. README.md / README_EN.md 补「按机器内存选挡」指导：auto 常驻热备（+174MB 换断网全量召回韧性）vs cloud（~280MB，无本地备胎）。
    2. README.md / README_EN.md 加冷备 v21.2 roadmap：本地模型不常驻、cloud 故障时加载 + 批量补算存量本地向量、degraded 窗口 ∝ 库大小（中小库实测十几秒）。
    3. 版本号补丁位五文件对齐（pyproject.toml / manifest.json / CHANGELOG.md / README banner 保持两段 v21.1）。
    4. 用例总数 2048 → 2048（纯文档补丁，无新增用例；--collect-only 实测本树）。

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

v21.1.0 (众神殿地基版 · 多 bot/多 profile 域隔离 + v21.0.1 会话补丁收口 · 2026-09-15)
    主题：**记忆有殿，人格独立——多 bot 各据一殿，跨殿不串味。**（定位①：单主人多分身）
    1. 众神殿殿注册表 + 管理 API（schema v8 pantheon_halls：创建/列出/查/停用软删，删殿不删记忆）。
    2. 跨殿借阅（schema v8 hall_grants：grant/revoke/list，可撤销可过期 fail-closed；主体=user_id 殿）。
    3. 借阅在 core 读路径真生效：SearchRequest 加 caller_user_id，recall_chain/session_search/dossier
       跨殿须持借阅（caller 空/==user_id 放行=读自己殿/主人直连）。
    4. WP-2 反思落对殿：plugin 建/结束会话带当前殿 user_id，session_end 反思不再跑 default 殿。
    5. WP-3 拒绝跨殿会话夺权：session_start 写入前 owner 检查，他殿占用即拒。
    6. WP-5 session_id 白名单校验 + 日志占位（拒空格/换行/URL·SQL 元字符）。
    7. WP-7 reflect 溯源上下文 token 配对复位（无 stale leak）。
    8. S5/S6 plugin 拼 URL quote() + MCP session_start 加回 session_id（三入口契约对齐）。
    9. WP-4 prune 读侧补域：不跨库比矛盾（读侧此前全库拉取）。
    10. WP-6 evolution UUID 跨殿脱敏：reason 与 origin 三件套只对拥有本殿事实者完整可见。
    11. WP-8 债务归真：caller 密码学绑定/WORM 在①定位改判「不适用」；F4–F9「三态开关/
        影子起步」措辞归真为「schema 就位·逻辑未接线」。
    12. WP-9 死代码登记：reflection_candidates/retrieval_weights/superseded_by 三空壳明标预留。
    13. WP-10 防御纵深：出身乘数查询走 scope_clause 补域 / 墓碑恢复列名白名单 / 健康探针非空判。
    14. 守卫：新增 tests/test_v21_1_session_domain.py 与 tests/test_v21_1_pantheon.py 红→绿对照。
    15. 用例总数 2034 → 2048（--collect-only）。
    保留边界：core 路由 user_id 仍自报——①下同一主人多分身、无外部越权威胁。详见
    CHANGELOG「## v21.1.0」段。

v21.0.1 (维护版 · 外部 Agent session 生命周期契约闭环 · 2026-09-14)
    主题：**会话生命周期双向闭环，拒绝静默丢弃。**
    1. ducky/routes_v8.py 的 /session/start 路由与 ducky/pipeline/memory_persistence.py 支持可选 session_id 参数，优先采纳外部 Agent 原生会话 UUID，未传保持 ses_* 兜底。
    2. integrations/hermes-plugin/aidumem/__init__.py 在 initialize 建立双保险契约，解析并对齐服务端响应的 session_id，确保 on_session_end 与后台反思链路 100% 畅通。
    3. 用例总数 2034 → 2034（--collect-only），tests/test_v20_broadcast_session_bank_scope.py 补齐路由参数递进与自定义 session_id 断言，四道门禁全绿。

v21.0 (正式版 · EchoMind 融改：认知治理全量版 · 2026-09-13 开工 · 2026-09-14 收口发布)
    主题：**记忆有出身、有户口、可导出；治理核心开关护航、影子起步。**
    维护者 2026-09-13 拍板一次性全量（施工任务书：wiki v21 文件夹；
    四份外部评估 + 施工方独立复核为据；EchoMind 无 LICENSE，只借思路不搬代码）。
    1. schema v6 总批次：facts.epistemic_mode（默认 'fuzzy'，存量不回填——
       宁缺毋滥）+ facts.superseded_by + knowledge_evolution 溯源三件套
       （兜底建表防新库 ALTER 落空）+ reflection_candidates /
       retrieval_weights 两新表（均带 (user_id, bank_id) 域键）。
       全部 additive；迁移总账 +4 迁移点同步登记。
    2. ducky/epistemic.py：resolve_epistemic 纯函数（零 LLM 成本 source
       映射：用户直述→user_provided / 外部引用→referenced / LLM 推断→
       reasoned / 兜底→fuzzy）；检索乘数四档默认 ×1.15/×1.05/×1.00/×0.85，
       env 可配、非法 fail-closed 回默认（env 注册表 +4 同步）。
    3. 删除链矩阵补两新表显式 clean 裁决并接线 §16 级联清理——
       候选草稿与偏好画像同样在擦除承诺内。
    4. 守卫同步：mkdtemp 基线 54→55；except 棘轮 632→636
       （v6 迁移 4 处容错，与 v5 同型纪律）。
    5. 用例总数 2002 → 2034（--collect-only），新增 41 条全部红→绿对照；
       独立开发机 2022 通过 · 12 跳过（2026-09-14 本树）。
    6. 在途（本段随施工推进持续更新，分项验收以任务书为准）：
       F1 写入路径/检索乘数/探针；F2 provenance 填充与审计端点；
       F3 dossier 导出；F4–F9 治理核心（三态开关：关/影子/开，影子起步）。
    7. v21.0 收口（2026-09-14，生产用户审计 2🔴3🟡3🟢 全闭环）：🔴-1 主链路
       sidecar memory_epistemic 打标（schema v7，infer 诚实映射）+ 打分
       回落链；🔴-2 via_federation 参数归位（共享底层不一刀切）；
       🟡-3 单源精确回填脚本（dry-run 默认，具名来源不碰）；
       🟡-1/2 口径诚实 + 端点 UUID 判据改判；🟢 三项全采纳
       （token 配对复位 / 探针多样性 / 结晶审批提示）。明细见 CHANGELOG。

v20.5.1 (维护版 · 四份审计整合收口 + 发布工程修复 · 2026-09-11)
    主题：**门禁必须真的在场；接缝必须真的接上。**
    四份审计（用户视角生产实测 / Sonnet / Luna / DeepSeek v4.1 Flash 自评）+
    维护者独立增量审计，逐条 file:line 复核后闭环：
    1. 🔴 CI 失防窗口根修：docker-context-secrets 的 grep -c 计数与退出码
       未解耦（无泄漏时 `|| echo 0` 让 leaked="0\n0"，门自 08-28 诞生起
       永不绿；09-09 首跑即红 → workflow 被禁用 → 次日正式版零门禁发布）。
       步骤修正三态（无泄漏绿/有泄漏红/构建失败红），本地 stub 三场景验证；
       scripts/push_gate.sh 焊第五道关：Tests workflow 非 active 立即停推。
    2. 🔴 联邦管理面接缝（根因 R-1 排查）：register/deactivate 补 caller
       门槛（本人或 admin）——upsert 曾可被他人复活已休眠 agent；
       list_agents 补 _require_caller（用户审计 🟡-1）；heartbeat/migrate/tiers
       三端点经评估「有意开放」，理由随码注释在案。
    3. caller↔凭据轻量绑定（T-07）：AIDUMEI_CALLER_BINDINGS（token 指纹 →
       可代表 agent 白名单），未配置时行为逐字不变；配置非法 fail-closed。
    4. 统一作用域 SQL 构建器 ducky/scope_sql.py + 棘轮守卫（183 处手拼片段
       只减不增，30 文件逐一带理由登记）；verbatim/conflict_resolver 首批迁移。
    5. 复杂度回吐：run_add_pipeline 53→7 / write_fact 44→10 /
       funnel_search 41→8（行为逐字不变，子步骤补单测）。
    6. WAL 崩溃/重放幂等矩阵：重放 N 次 == 重放 1 次的不变量入测试。
    7. 打分正确性：scoring.py 三处 `or` 吞显式 0 修正为「缺失才兜底」；
       mem0_sync md5→sha256；/health 新增 mem0 路径一致性探针
       （DATA_DIR 与 qdrant path 脱钩时告警——DEPLOY_DOCKHOLD 记录在案的坑）。
    8. 文档归真：README 头条拆分行为 1787 + 脚本行为 70 + 守卫 136
       （口径脚本可复算）；docs/archive/ 建立，v14 时代 ARCHITECTURE 等
       四份历史文档移入；POSITIONING 租户行加脚注、「10–20 分」标注非实测；
       ONE_LINE_INSTALL 收敛子项经复核驳回（逐字相等守卫钉死，无漂移面）；
       结案陈词「前端零触碰」勘误（4632e4e 实为 8 文件 +977/-118）。
    9. 生产侧随部署执行：潮浪 cron prompt 两处 curl 补 Bearer（用户审计 🔴-1）；
       生产 facts.db 清理 3 条 smoke_sandbox grants 残留（用户审计 🔴-2，先备份）。
    10. 用例总数 1888 → 2002（--collect-only），新增用例全部红→绿对照；
       独立开发机 1981 通过 · 12 跳过（2026-09-11 本树）。
    11. 已知瑕疵如实登记：tests 子集选择（-k）下 test_jia13_verbatim 存在
       顺序依赖（全量套件不受影响，随测试重组一并治理）。

v20.5.0 (正式版 · 三方评审整改收口 · 2026-09-10)
    主题：**说出口的承诺，必须实测成立。**
    三方评审（用户视角端点复现 + Sonnet 5 / Luna 代码审计）各推翻半个核心卖点，
    逐条 file:line 核验全部成立，本轮闭环：
    1. 🔴 谱系串链根修：upsert 改 RETURNING id / 唯一键回查（绝不信任
       lastrowid）；verify 端点补事实行存在性 + 链尾内容对账（幽灵链不再
       报绿灯）；UNIQUE(memory_id,version) 兜底；删除路径补 DELETE 终链；
       diff_summary 去除 fact_key 明文；存量行经 schema v5 回填哈希并补
       BACKFILL 基线（如实声明历史不可追）。
    2. 🔴 授权闭环：grants 三端点强制 caller 校验（本人或 admin），
       created_by/revoked_by 从 caller 派生；空 caller 默认 403，逃生门
       AIDUMEI_ALLOW_IMPLICIT_CALLER=1 显式过渡；谱系/授权查询端点补租户
       归属校验；expires_at 非法值创建即拒、存量按过期处理；未知 scope
       维度 fail-closed。
    3. 措辞归真：「不可篡改」改「可检测篡改（tamper-evident）」——强不可抵赖
       （签名/外部锚定）入 v21 路线图，不在本版承诺。
    4. 工程面：dependency-audit 周 cron 定时化；Dockerfile 补 HEALTHCHECK
       （打 /livez）；新增 scripts/lineage_ghost_cleanup.py 供 v20.5.0a 升级者
       清理幽灵链（默认 dry-run，--apply 先备份再清理再对账）。
    5. 用例总数 1857 → 1888（--collect-only），新增 31 条守卫全部红→绿对照
       （先写复现缺陷的测试，再修到绿）。
    6. 残留边界如实写明：caller 身份仍为自报参数，未与凭据密码学绑定——
       本版闭环防的是同一可信宿主边界内 Agent 的越权与误操作；token→身份
       绑定属 v21。

v20.5 (Preview 预览版 · 可信联邦授权与记忆谱系 + 用户审计整改 · 2026-09-10)
    公开身份 = 20.5 Preview（tag/Release 均为 v20.5-preview），面向外部用户与
    专家开放使用与审计，收集反馈后再升格为不带后缀的正式版本号。
    主题：**从「认知与混合检索引擎」向具备「可信授权治理与密码学级谱系溯源」的可信记忆控制平面跨越。**
    1. 吸收 Walrus 调研精髓，彻底告别仅 shared: bool 的粗粒度标记。
    2. 新增 federation_grants 表与 ducky/federation/grants.py 细粒度授权引擎：
       基于 grantor/grantee/scope/actions/expiry 的零信任访问控制，跨 Agent 越权
       访问默认拒绝（403），支持授权即时撤销（revoke_grant）与自动过期失效。
    3. 新增 memory_lineage 表与 ducky/memory_lineage.py 密码学谱系账本：
       每次事实新增/冲突消解/自演化覆盖，计算 SHA-256 内容散列并链接父哈希，
       形成可检测篡改（tamper-evident）的链式演化历史（version + previous_version_hash）。
       v20.5.0 正式版起 verify 端点补「事实行存在性 + 链尾内容对账」，链不再能
       指向不存在的事实，也不再对绕行改写报绿灯。
    4. facts 表幂等扩充 content_hash/version/previous_version_hash/last_actor。
    5. PEP 织入（federation/routes.py _enforce_grant）：recall/facts-add/
       broadcast/awareness 四端点接受 caller_agent_id，跨 Agent 读写无有效
       Grant 一律 403；单机/本 Agent 回环零破坏放行。
    6. 谱系射程补全：dedup.apply_merge 与 /facts/add 端点补 hash/version
       推进与 lineage 同事务记录（次路径不再绕过版本链）。
    7. 配套登记与守卫对齐：DELETE_CHAIN_MATRIX 豁免登记、_MIGRATION_LEDGER
       迁移点登记、write_endpoint_budgets 路由台账、logger 契约处数、
       except 棘轮基线（609→626，谱系/授权降级钩子）、mkdtemp 位点数基线。
    8. 用例总数 1837 → 1888（--collect-only），Preview 期新增 20 个针对性用例 +
       正式版整改新增 31 条守卫（谱系身份/授权闭环/存量回填/crud 端到端，
       全部红→绿对照）。四环实测数字以 2026-09-10 正式版归档为准。
    9. b 阶段用户审计整改 🔴-1（grants.py _match_scope）：scope 限定维度
       （category/tier/tag/user）调用方未提供时旧逻辑跳过 → 限定被当通配。
       改 fail-closed 白名单：缺维度一律拒绝，端点面同步堵死。
    10. b 阶段用户审计整改 🔴-2（grants.py create_grant）：INSERT OR REPLACE
       允许已撤销/已存在 grant_id 覆盖复活。改显式冲突检查——撤销是终态，
       重授权必须新 ID，审计链不断。
    11. b 阶段用户审计整改 🟡-1（hot/crud.py /update）：按用户审计裁决 fact_value
       正文变更与 federation writer 同字段，补 hash/version 推进 + lineage
       同事务记录。
    12. UI 修复（登录页样式塌陷）：CSP style-src 'self' 打死 login/index 内联
       <style> 元素（登录页无样式白板，生产实锤）——样式收编 css/style.css
       （1029→1162 行），缓存戳 v=6/v=10。
    13. 守卫补射程（第五次发作）：TestNoInlineStyleInFrontend 新增 <style>
       元素形态断言（剥 HTML 注释扫描防字面误伤）；except 棘轮 626→627
       （+1 crud /update lineage 降级钩子）；🟢-2 grantor 校验转正式版前评估。
    14. 生产实测已归档（2026-09-10）：独立开发机 1845+12 / 基础路径 1821+25 /
        生产机沙箱 1877+11 / 全轴齐备 1844+1，总数 1857（--collect-only）。
        详录见 CHANGELOG.md「## v20.5」段。

v20.4.0 (正式版 · 三方审计 P0/P1 整改 · 断点续修四环复测收口 · 2026-09-09)
    详录见 CHANGELOG.md「## v20.4.0」段。
"""
from __future__ import annotations

# ── f 世代版本规格（f0.1 起，2026-09-23）──────────────────────────────
# 对外版本号统一为 ``f*.*``：f = future / fantasy / forever。
# Tag 与 Release 一律用它。
#
# 为什么还留一个数字版本：``f0.1`` **不是 PEP 440 合法版本**（必须数字开头），
# 放进 pyproject.toml 会让 `pip install` 直接失败。故分两层——
#   SERVICE_VERSION：数字版本，供 pyproject / manifest / 包管理（技术真相源）
#   FULL_VERSION   ：对外品牌版本，供展示 / Tag / Release（对外真相源）
# 两者一一对应，由 LINEAGE 第三列钉死，不许各走各的。
SERVICE_VERSION = "0.1.0"
FULL_VERSION = "f0.1"
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
    ("0.1.0", "", "f0.1", "f 世代首版 · LoCoMo 跑分整改：verbatim 事件时间根因修复（写线/读线/时间衰减防回归）· 2026-09-23"),
    ("22.0.0", "", "v22.0", "雷霆审计整改 · 众神殿鉴权/绑定 strict/注入边界/逃逸门组合闸/治理多语言/有界评估池/依赖合一/产品面收口 · 2026-09-20"),
    ("21.2.0", "", "v21.2.0", "Memmy 融改 · 回声抑制/MMR/错误签名/轨迹级奖励 · 2026-09-16"),
    ("21.1.1", "", "v21.1.1", "文档补丁 · 内存挡位选择指导 + 冷备 v21.2 roadmap · 2026-09-15"),
    ("21.1.0", "", "v21.1", "众神殿地基版 · 多 bot/多 profile 域隔离（读侧补域/evolution 跨殿脱敏）+ v21.0.1 会话补丁收口 · 2026-09-15"),
    ("21.0.1", "", "v21.0.1", "补丁版 · 外部 Agent session 生命周期契约闭环（/session/start 接收 session_id）· 2026-09-14"),
    ("21.0", "", "v21.0", "正式版 · EchoMind 融改认知治理全量版 + 生产用户审计收口 · 2026-09-14"),
    ("20.5.1", "", "v20.5.1", "维护版 · 四份审计整合收口 · CI失防根修 · 联邦接缝与作用域构建器 · 2026-09-11"),
    ("20.5.0", "", "v20.5.0", "正式版 · 三方评审整改收口（授权闭环/谱系身份/存量基线）· 2026-09-10"),
    ("20.5", "", "v20.5-preview", "Preview 预览版 · Grants+Lineage+用户审计整改+UI修复 · 2026-09-10"),
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

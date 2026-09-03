"""
ducky.version — aiduMEI 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

v20.3.2 (正式版 · 五方外审整改 · 一致性与底层 · 2026-09-03)
    主题：**pre 修的是「代码算错了」，beta 修的是「默认值是错的」，正式版修的是
    「边界不成立」—— 串行、正常、凭据齐全的路上全绿；换到并发 / 异常 / 中文 / 浏览器 /
    真 uvicorn / 长期运行，边界就不在了。**
    五方外审（用户方 + Gemini 3.8 Flash + GLM-5.3 + GPT-5.6 + Kimi-k3）34 项发现全部坐实、
    零假指控；本版按《外审后任务书》全部闭合。不升 20.3.3：pre → beta → 正式，版本身份不变。
    1. P0-4/P0-5（GLM F-1/F-2 · Codex F-08）逃生阀 AIDUMEI_TRUST_PROXY 在**真 uvicorn**下恒 503：
       uvicorn 默认 proxy_headers=True 用 XFF 的**值**改写 client.host —— 全仓「不采信 XFF 值」
       被启动参数一行推翻；TestClient 不经过那一层，所以 9 条守卫全绿。main() 显式
       proxy_headers=False。中间件真实顺序与注释相反：鉴权最外层，401/503 不进计数、不带
       安全头 —— 物理调序。**纪律：凡与请求来源 / 请求头 / 中间件层次有关的判据，一律起真
       uvicorn.Server + 真 socket 验；TestClient 只许测路由内部逻辑。**
    2. P0-1/P0-2/P0-3（用户审计 A/B/C）report.py 自报的 commit 不在任何 tag 血脉上却退出 0 →
       git describe + anchored 判定，脱锚退出 2；/health 匿名视图**留键说明不删键**（一行
       Prompt 第 7 步要读的 data_dir_writable 对未带凭据的 agent 仍可读），门禁未启用时全量
       开放，匿名结果 30s 缓存；acceptance 的「hard gate: push_gate exits 0」只测执行位 → 真跑；
       push_gate 改 bash + 解释器探测（生产只有 bash 与 venv/）。
    3. P1-7 MCP 14 个工具补 bank_id（v20 最核心的多域隔离此前在 Claude Desktop / Cursor / Hermes
       生态里不可达）；P1-11 top_k 加 1–100 边界并给后端 fan-out 硬顶；P1-13 配置损坏 ≠ 空配置
       （ConfigUnreadable → 409，原子写带 .bak）；P1-14 main() 不再自己起后台（lifespan 唯一
       负责人，策略检查前零副作用）。
    4. P1-8 中文词元：原正则把整句连续汉字当**一个** token，多词中文查询词法分恒 0 →
       中文 bigram（与 FTS trigram 两个口径一个理由：索引压误召回，重合分要区分度）；
       旧名 calc_bm25_score 自家零调用（守卫）。
    5. P1-9 事务卫生：_ConnProxy.__exit__ 由裸 pass 恢复 sqlite3 契约（异常 rollback / 正常
       commit）；借出时发现悬挂事务**出声** + /health 计数（不自动回滚：同线程嵌套借用合法）；
       db_tx() 供新代码；无 rollback 写函数棘轮 96 → 93 只降不升。
    6. P1-10 幂等：claim 由 SELECT→INSERT 两步改 INSERT…ON CONFLICT DO NOTHING 原子抢占（barrier
       并发双 new 验红）；pending → 409 不再继续写；finalize 失败释放 key，而不是把合法重试
       锁死 10 分钟。
    7. P1-12 无凭据模式 Host 校验（Codex F-04 · DNS rebinding）：Host ∉ 回环 / AIDUMEM_HOST /
       AIDUMEI_TRUSTED_HOSTS → 421；浏览器跨站写（Origin 非受信 / Sec-Fetch-Site: cross-site）
       → 403；无 Origin 的 CLI / MCP / cron 不受影响；有凭据或 TRUST_PROXY=1 时让位。
    8. P1-15 环境变量注册表（用户审计 H / GLM F-3）：双前缀 90 个名字单一真相源，AST 抽取与注册表
       **完全相等**（新增忘登记 / 登记不存在的名都红），启动期对未知 AIDUME?_ 变量 WARNING +
       最近似名；动态前缀单独登记；salience 两个名字此前由 f-string 拼出、从未文档化 →
       改字面量并采用当前前缀 AIDUMEI_SALIENCE_*。
    9. P1-16 drill 5 项判据 4 项恒真 → 脱敏 / 空探针必 fail 并说明原因；P1-17 WAL compaction
       （pending 全留、终态折叠、tmp+fsync+replace 原子替换、体积触发带滞回、对账后收敛）；
       P0-6 mem0ai 2.0.19 → 2.0.20 双文件对齐 + 补丁台账上 /health（probes.mem0_patches）。
    10. P2 批次：互斥规则逃生阀真接线（DATA_DIR/conflict_rules.json）；七循环 + coalesce 刷写器
       可中断睡眠 + lifespan 停机 join；口令哈希创建即 0600；召回 INFO 日志不落 query 原文；搜索错误信封
       error_code / retryable；注入拒绝文案给出路（如实：/add/raw 同受守卫）；水位告警如实说明
       refine_memory 无 LLM 时有损；两条生产红的用例改为自造世界；沙箱缺席轴登记表增加
       特征识别对照；TROUBLESHOOTING +2 条、OPERATIONS 基座升级仪式、README 数字表读表口径。
       logger 契约处数 93 → 95（+env_registry +idempotency）。
    11. 用例总数 1573 → 1726（上一发布 v20.3.1 → 本发布；`--collect-only` 2026-09-03 实测；
       pre / beta 两个阶段快照均为 1649）。各形态通过 / 跳过数见 README 表，全部随本树重测
       后填数，不沿用旧日期。

v20.3.2 (beta 阶段快照 · 七方外审整改 · 默认配置面 · 2026-09-02)
    主题：**前十轮修的是「代码算错了」，这七份翻出的是「默认值是错的」。**
    三条最重的发现——反代裸奔、迁移顺序、绑定来源——代码逐行都对，
    错在默认配置与真实部署形态的接缝。「首跑没问题」不等于「按标准形态部署没问题」。
    1. P0-1（外审 C-1）默认安装 + 同机反代 = 全站零凭据可读写。自动生成口令
       source=auto 不启门禁 → 无凭据实例「只服务回环」→ 而同机 nginx 的对端
       恒为 127.0.0.1。判据升级为「回环 且 无反代痕迹」；只判代理头**存在性**
       不读其值（值可伪造，存在性不可否认）；逃生阀 AIDUMEI_TRUST_PROXY。
    2. P0-2（外审 P1-A）memory_types 唯一域索引建在补列**之前** → 存量表抛
       no such column 被最外层 except 吞掉 → 整段初始化中止、三列一列没补、
       _checked 永不置位（按请求调用 = 热路径无效功 + 日志洪水）。F-16 的租户
       隔离防护在唯一需要它的场景失效，藏了 5 个 Tag。**顺序即语义。**
    3. P1-3（外审 P0-01）WAL 启动对账从不闭合原条目 → 永久 pending、每次重启
       重放、recovered 是假账。订正：级联删除幂等，故非数据损坏而是账本不收敛。
    4. P1-4（外审 P1-B）注入防御误拒真实载荷（本机实测 7/12，比外审报的更宽）：
       裸 [system]/[prompt]/<system> 命中、Markdown 表格分隔行与真实错误日志被判
       重复行攻击。收窄为词组共现 + 剔除结构性重复行 + 阈值抬到篇幅填充量级；
       建 FP/TP 双向门禁；ChatML <|system|> 精确保留；新增 injection_rejections 探针。
    5. P1-5（外审 P1-C）MCP session_end 发 body 而端点从 query 绑定 → 恒 422。
       同型缺陷在 ad3ba6c **同一提交**修了脚本侧、漏了 MCP 侧。守卫升级为按
       FastAPI 依赖图判**绑定来源**（旧判据只比对键名，看不见 query/body 之分）。
    6. P1-7（外审 F-2）tests/ 从来不在静态关射程里，一个真实 F821 在逃。
       **守卫的第四种坏死法：地理射程落后于缺陷分布。** 射程加 tests/，
       F841 基线拆分登记（合成总数会让两侧互相遮蔽）。
    7. P2 批次：明文口令不再进 os.environ（M-2）；补齐四个安全响应头 + CSP（F-4）；
       消解规则移除脱敏占位符（两条死规则）；calc_bm25_score → calc_token_overlap_score
       （**全仓两个同名函数都不是 BM25**，一起改）；单进程契约落成启动期守卫并
       接进 lifespan 与 main 两条路。
    8. 用例数由 1589 增至 1649（阶段快照）。本机 1637 passed + 12 skipped（2026-09-02 实测）；
       基础安装路径 1617 passed + 32 skipped（同日干净 venv + 真实 clone 实测）。
    9. 脱敏：公开大仓全历史身份重写（94 个提交的 author/committer + 1 处提交信息
       部署域名），内容树逐字节未变，三面归零并由全新克隆独立复验。
    10. 驳回三条外审判断：/health 「免费侦察器」（脱敏白名单已在）；inotify_simple
       「构建阻断」（纯 Python wheel，另一审计员 macOS 实装成功）；PBKDF2 阻塞
       「100–200ms」（本机实测 15.4ms）。**审计报告也要过实测。**
    11. 已观测未闭合：test_jia16 在全量下出现过 1/4 轮间歇性红灯（单跑与后续
       3 轮全绿）。机制假设是 caplog 捕获全局 logger 后被异步日志污染。
       **不宣布已修**，登记待查。

v20.3.2 (pre 阶段快照 · 第 10 轮独立审计整改 · 零凭据首跑 · 2026-09-01)
    主题：换一个模型、脱离本地笔记、把公开仓重新克隆到干净环境里审一遍。
    结论：代码是上游水平，问题全在代码与文档的接缝处 —— 三条主要发现没有一条
    是「代码算错了」，全是「文档说的和代码做的在某个特定环境下不一致」。而那个
    特定环境，正是新用户会撞上的第一个环境：前九轮审计全部跑在凭据齐全的机器上，
    「后端未配置」这条分支从来没被任何一次审计走过。
    1. P0-1 单条删除把「后端未配置」当成关键层失败 → 零凭据下回 500，而同环境
       /delete_all 回 200：两条链共用三态契约却对同一信号给出相反判决。判据只放在
       「取后端」这一步（与 delete_all 结构逐字对齐），拿到后端后的任何失败照旧算失败。
    2. P0-2 /graduate 把 503 吞成 500：HTTPException 是 Exception 子类，被宽 except
       重包。补 `except HTTPException: raise` 透传 —— 这个惯用法仓里已有 6 处，
       漏的那一处恰在新用户必经之路上。
    3. P0-3 README「这 12 条不是玄学，自己就能验」配的命令只装 pytest+pyyaml，
       实测产出 31 条跳过。命令与数字现在同屏、各配各的环境。
    4. P1-5 零凭据全路由扫描固化为常驻测试（子进程 + 最小 env，不继承父进程环境）。
    5. Rev.2 改用初始化边界专用类型：仅配置文件缺失或完整样例仍为占位凭据时抛
       Mem0NotConfiguredError；两条删除链只捕获该类型。配置损坏、嵌套形状不完整、
       凭据/网络/代理/Qdrant/SDK 故障仍按真实 503 失败处理；旧文件探针退出删除判据。
    6. 元守卫：AST 扫「try 内取后端 + except Exception 抛 500 + 无 HTTPException 透传」
       的块，命中数必须为 0（本轮修前 = 1）。
    7. 守卫世界模型修正：沙箱推导式仍把 git 轴算作缺席，而沙箱自 v20.3.1 起是
       bundle clone（有 .git）—— 守卫报「README 写错了」而 README 是对的。
       本仓第三次「守卫的世界模型落后于它守的现实」。
    8. 用例数由 1573 增至 1649（阶段快照）。本机 1637 passed + 12 skipped（2026-09-02 实测）；
       基础安装路径 1617 passed + 32 skipped（同日干净 venv 实测）；生产机沙箱与
       全轴齐备两行待本树复测后填数。
    9. 被实测推翻的猜测一：「字段拼错会静默写入空记忆」→ 实测 422，必填校验正常。
       extra="allow" 的代价只在可选字段，且是有意的别名兼容 —— 记为已知限制，不修。
    10. 被推翻的猜测二：「DELETE /delete 别名没注册」（拿到 405）→ 路由确实在场，
       是我用了 client.get() 而非 .request('DELETE',…)。探针有错，不是产品有错。
    11. 被推翻的猜测三：「配好 mem0 后单条删除就正常」→ 替身缺 scroll API，
       代码正确地拒绝删除并报「归属无法确认」。fail-closed 反而增加了证据。
    12. 被自家守卫抓住的错：扫描子进程写了 AIDUMEI_DATA_DIR，而 utils.py:97 读的是
       AIDUMEM_DATA_DIR（冻结兼容前缀），变量名错被静默忽略，测试库写进了仓库 data/。
       改最小显式 env 并实测「仓库 data/ 未被触碰」。

v20.3.1 (九份审计整改 · 仪器读世界 · 2026-09-01)
    主题：v20.3 正式版发布后的 9 份审计（用户审计（生产实机） + 8 个外部模型，约 60 项发现，
    逐条核验零假指控）整改轮。纲领一句话（Qwen3.7-Max 审计原话）：每一层仪器
    都在读上一层的输出，没有一层去读世界——全部验收判据落在系统真实状态上。
    1. crontab 三连修 + latest 解析：去重键 9装1 / 幽灵任务 / -x 不查 PATH；--installed 数真实 crontab，install 尾与世界对账
    2. 半开探针一次性（_PROBE_TIMER 永不置空）→ 重挂修复 + LLM 腿覆盖
    3. agent_integration_check 双空断言（GET body 被丢弃 / count 整串恒 0）
    4. drill --run 从未跑通（stdin 之争 + 顶层键不存在）→ mock 实测通过
    5. 幂等断裂：local/lite/async 六条早返回补 finalize；/add/raw 幂等键；Idempotency-Key header 支持
    6. 测试数字口径：三处打架收敛为同源双口径
    7. acceptance：第 18 项改锚不变量（版本感知）+ 三道硬门槛真跑
    8. 一行 Prompt 真源收敛：展示区改引用形态 + acceptance 对账
    9. report.py：无凭据恒 exit 2 / engine_mode 恒 null / 实装数优先
    10. 用例总数 1552 → 1573。本机 1561 passed + 12 skipped（2026-09-01 实测）

    发布次日晨检（2026-09-02，不升版本号）：
    11. 沙箱备胎跳过的诊断改到能自曝真因：模型一直在机器上，缺的是测试
        进程没继承 AIDUMEI_LOCAL_EMBED_CACHE（单变量对照钉死：只设这个变量
        就通过）。旧文案「模型文件未部署」会把人支去重跑 fetch，而那不是解法。
    12. mcp_extra 轴探测器接错线：问的是 `import fastmcp`（PyPI 上另一个同名
        项目），产品 import 的是 `mcp.server.fastmcp` —— 装了 mcp 也永远报缺席，
        这条轴在任何机器上都不可能齐备。改 find_spec 探产品真正 import 的路径。
    13. 「全轴齐备」由推导升格为实测：1573 passed · 0 skipped（2026-09-02
        生产机，五轴齐备，专用测试 venv，服务 venv 全程未动）。
        推导对了不等于测过了；守卫改为实测形态优先，防旧日期冒充新结论。
    14. 维护任务跑满一周期：实文 8 条、intended=installed=8、02:30 备份真实
        执行且 sha256 复验通过；三条周日任务不等周日，只读预跑 latest 路径双 PASS。
    15. 指纹守卫没被耸肩放过：全轴跑后报「有变化」，逐文件定性为主库未变、
        变化聚在 08:15:03 整点（e2e_smoke cron），测试写入落在树内隔离目录。
    16. README 矩阵 ②③ 两行补日期（1499 总数时代基线，与守恒式矛盾），
        守恒句改为 ①④⑤。

v20.3.0 (优忆思 · 入口与可操作性 · 2026-08-31)
    主题：修复 v20 的入口债。11 份双盲 VOC 给出同一个结论——
    「代码是上游水平，文档是反 agent 的」：11/11 不建议投产、
    部署度 50.5、运维度 37.1、推荐意愿 48.5。v20.3 不加新记忆能力，
    只做一件事：让陌生 agent 和用户从仓库本身就能装好、验好、接好、
    养好。验收以机械命令与退出码为准，不以文字描述为准。

    预备阶段（基线卫生）：
    1. 收编上一轮未入仓的账本记录（CHANGELOG 与本文件同步），
       编号由漂移守卫强制连续。
    2. WP-A 三条真投产阻断：rerank 样例从代码不认的扁平形状改为
       嵌套形状，并用真实 loader 吃样例；/health 删除恒绿
       injection_guard_ok，port_service 改为真实 socket 探测；
       restore_backup.py 去掉 2026-07-27 硬编码路径，改为显式
       snapshot 参数、支持 dry-run/limit、退出码可判定。
    3. 一致性面：Python 版本、MCP 端口、鉴权口径、lite/云重叠
       口径、AIDUMEM_BACKUP_ROOT 默认值、ECharts 加载口径全部
       对齐源码/实测事实，并补守卫。
    4. WP-B 生效自证：新增 scripts/e2e_smoke.py，走 HTTP 做写入 →
       flush → 新请求召回 → trace → cleanup，输出 PASS/WARN/FAIL
       与稳定 JSON，退出码 0/1；测试覆盖健康失败、配置缺失、
       召回失败、partial 清理等分支。
    5. WP-C 入口重构：新增 AGENTS.md（3.2KB）、llms.txt、
       scripts/README.md；README 1102 行 → 595 行，主 README 只留
       主路和导航，安全/竞品/跑分/谱系移入 docs/ 独立档案。
    6. 历史叙事外移：docs/SECURITY-AUDIT-LEDGER.md、
       docs/POSITIONING.md、docs/BENCHMARKING-POSTURE.md、
       docs/VERSION-LINEAGE.md；主 README 留结论和链接。
    7. WP-D/WP-F：新增 TROUBLESHOOTING.md（10 场景）、
       docs/HEALTH.md、docs/OPERATIONS.md、
       docs/AGENT_INTEGRATION.md、docs/BACKUP_RESTORE.md；
       ARCHITECTURE.md 标注历史快照，不再冒充当前事实源。
    8. 机械验收聚合脚本 scripts/acceptance_check.sh：入口文件、README
       行数、AGENTS 体量、rerank 样例、假探针、恢复脚本、MCP 端口、
       e2e 脚本可执行，一处失败即非零退出。
       用例总数 1552 → 1573（本机 1561 passed + 12 skipped · 2026-09-01；
       生产机沙箱待复测 —— 数字与日期一体，换树必须重测）。

v20.2.5 (两份审计整改 · 2026-08-28)
    主题：用户视角实机审计（20 项）+ 第三方独立外审（评级「有条件不通过」）。**六项落点全是修复，没有新能力** —— 所以版本号只走三级。
    公开 Tag/Release 停 v20.2；**小仓打 Tag v20.2.5 + Release**（本版起的
    SOP 增补：小仓承担版本历史可回看的职责）。
    1. A1 外审 F-03（P0）：**v20.2.4 那条「refine 候选跨 bank 收窄」是假修复**
       —— 算出了 tenant_clause 的子句和参数，**SQL 里一个字都没拼**，而注释
       写着已修、结案陈词列为已修、且没有任何测试盯着。本版两个分支（默认
       身份 + 具名租户，后者此前连 bank 参数都没用到）都真的拼进 SQL；
       apply 的摘要行从账本继承 bank，不再硬写默认域。判据全部是**集合相等**
       （{A,C} 而不是「返回了 A」—— 后者对「B、D 也混进来」毫无区分力）。
    2. A2 外审 F-02（P0）：删除结果三态 committed/partial/failed。此前任何层
       失败都 mark_status(committed) + status="ok"。**出口那端还断了第二次**：
       HTTP 层硬编码 `{"status": "ok"}`，连 v20.2.4 加的 not_cleared 都从没
       到达过调用方 —— 那条修复因此也是半假的。现在 committed→200 /
       partial→207 / failed→500；failed_layers（实际失败）与 not_cleared
       （预声明豁免）分开报；WAL 只在全绿时 committed，否则留 pending 可重放。
    3. A3 外审 F-01（P0）：运行目录交接。Dockerfile / systemd 显式设
       AIDUMEM_HOME / DATA_DIR / LOG_DIR / CONFIG_FILE —— 变量支持 utils.py
       一直有，**缺的是交付模板没设**：wheel 装进 site-packages 后数据落进
       包目录，而 bind-mount 的是 /app/data，两者不一致**没有任何症状**。
       /health 新增 runtime_paths 报实际打开的路径与可写性；CI 加
       wheel-runtime-dirs job（只读 site-packages 下真写一次库）。
    4. A4 用户实测 4🟡 + 3🟢：空 query 返回 recall_verdict="empty_query" 而非随机
       记忆；limit 加 Field(ge=1,le=100)、query 加 max_length（此前 limit=-5
       与 999999 都直通）；DELETE /delete 别名**收 query 参数**（只加装饰器
       是假修 —— DeleteRequest 是 body 模型而 DELETE 按惯例不带 body）；
       WAL 告警阈值 1MB→64MB（用户实测 checkpoint 返回 (0,N,N) 数据早已落盘，
       旧判据报的是「SQLite 就这么工作」，**告警恒真等于没有告警**）；
       feature_failures 带 last_failure_at；window=-1 显示为 idle；
       distillation 加人话字段（机器可读的 code 不动）。
    5. A5 工具面：**Ruff 此前根本没装**。装上当场抓到 F821 —— legacy_helpers
       用了未定义的 `_os`，那行一执行就 NameError、被 except 吞掉，于是那段读
       manifest 的配置逻辑**从来没成功执行过**（与 MCP facts_add 同型）。
       F821/F811 进 push_gate 第四道关阻塞，F841 走登记制（基线 10，存量里
       混着无害残留与疑似真缺陷）；风格类不接，接了只会毁掉门禁信噪比。
    6. A6 README 测试数字口径：1445/12 是完整 extras 环境；外审按 README 教的
       基础安装路径实测 1415/27 —— 数字没错，**口径没写**。两套都写明。
    7. 判据坑一（自查）：把「表不存在」当成层失败 —— 那会让任何没启用观察库/
       场景库的部署**永远拿到 partial**，与本版修的 WAL 告警是同一种病：
       告警恒真等于没有告警。改用仓里现成的 bank_contract.is_legacy_schema_error。
    8. 判据坑二（被自己的故障注入用例当场戳穿）：用「配置文件是否存在」判断
       后端未配置 —— mock 掉删除函数让它抛异常时，判据却因为本机没有配置文件
       而放行。**那等于生产上 Qdrant 一断，删除就报成功，正是 F-02 的原病。**
       改为看**异常来源**：取后端失败＝未启用，拿到后端删除失败＝真失败，
       两个 try 分开，两半各有一条用例盯着。
    9. 假绿灯（沙箱实测抓出）：新加的 Ruff 守卫**在没装 ruff 的环境里静默变绿**
       —— 生产 venv 不装 lint 工具，第一版 _ruff() 在工具缺失时返回空列表，
       于是 F821/F811 那条守卫报「零命中，通过」。**「扫过了」和「扫不动」
       长得一模一样**，比没有守卫更危险：它还替你签了字。改成显式跳过
       （tests/test_v20_2_5_audit_remediation.py）。
    10. 判据无区分力：第一版写「returncode 不在 (0,1) 就跳过」，而 python -m ruff
       在模块缺失时返回码**也是 1**，与「有命中」撞在一起。由「把 ruff 目录
       藏起来再跑」这条负向对照抓出（期望 2 skipped，实得 1 failed）。改用
       find_spec，并抽成 ruff_available() **供闸门与跳过轴探测器共用一份**
       （v20 有过探测器另写判据、两边射程不同的前车之鉴）。
    11. 由此新增**第十二条跳过轴**（ruff 已安装，门控 2 条）：登记表、两份 README
       的轴表、生产机沙箱数字（1496 通过 · 3 跳过）全部同步。普查也扩了能力
       —— 原先按行号归属数用例，跳过语句写在共用辅助函数里时**恒得 0**，
       报错却指向「位点搬家」，看起来像轴废了；现在做调用图不动点传播
       （tests/test_v20_skip_axis_census.py）。
    12. README 数字守卫的世界模型也落后一条轴：它把「git 轴门控数」直接当成
       「沙箱跳过数」，第十二条轴一出现就算出 1459/1、实际 1496/3，**报出来的
       样子像是文档写错了**。改成显式列出沙箱缺席的轴并求和
       （tests/test_v19_4_1_audit_fixes.py）。**假红灯和假绿灯一样害人。**
       同时撤回一句我自己加的话：「全轴齐备」那行写着「2026-08-27 实测」，
       而那天测的是十一条轴上的 1440 —— 数字被改成 1499 却没重测。本版在
       候选树上**重测**（旁挂 ruff，不写生产 venv）。
    13. D1（生产实机部署后的冒烟，P0）：**`/add/raw` 返回的句柄删不掉它自己创建
        的记忆，而删除报成功**。`DELETE /delete?memory_id=raw-…` 回 200 ok，
        向量与 facts 全留着、原文照旧可召回；换成 mem0 UUID 才真删掉。
        根因：`/add/raw` 走 mem.add(infer=False)，mem0 自己铸 UUID，句柄与
        向量唯一的连接是 metadata.content_hash，而删除链只比 id。这是
        v19.4.1 修 `verbatim:` 句柄那次的另一半 —— **只加了一个前缀分支，
        没问「产品还发出过哪些句柄形态」**。换算独立成 _raw_handle_hash，
        反查严格限在同域枚举结果内。
    14. D2：**单条删除没有三态 —— F-02 我只修了 delete_all**。
        cascade_delete_memory 两个 return 都硬写 ok，/delete 出口再硬写一遍。
        外审点的是全量删除，我就只修了被点名的那条链路 —— **原则 P5 在修复
        层面失效**：原则写对了，执行时只照着清单打钩。现在两条路径共用同一套
        判据（失败登记器与关键层集合提到模块级，不许抄第二遍）。
    15. D3：**`/add/raw` 的 facts 行也从没被删掉** —— 它落的键是
        `raw:<content_hash>`（raw_drawer.py），而删除链拼的是
        `raw:<完整句柄>`，永远差一截，实机 details["facts"] 恒为 0。
    16. 新增 `not_found` 态：一层都没命中时状态不再说 ok。HTTP 仍走 200
        （DELETE 按 REST 惯例幂等，consolidator 正在批量删「早就不存在的
        东西」），**变的是状态字段不再说谎** —— D1 当初就藏在那句 ok 底下。
        判据用「真删掉了几个」而不是「SQL 跑过了」：details["fts"] 是布尔
        「执行过」，拿它判命中会把「跑了但 0 行」算成命中，守卫立刻变白护栏。
    17. `not_found` 第一版是**白护栏**，生产实测当场拆穿：判据里算上了
        local_vector_deleted，而 dual_index.py 的 delete_local
        **return len(point_ids)** —— 报的是「请求了几个」不是「删掉了几个」，
        删一个不存在的 id 也回 1，判据恒真。同模块的 delete_local_by_scope
        一直是对的（先 count 再删）—— **两个孪生函数，一个诚实一个不诚实**。
        改成 retrieve 核实后报数；核实不了时照旧删除、计数回落请求数并打日志
        （宁可多报一个数字，不许悄悄少删一个点）。
    18. 社区 Issue #5（大仓，网友 Agent 提报）：**召回链路没有任何分数闸门**，
        零命中/弱命中条目能靠时效/可靠性/访问热度凑分填满结果集。属实。
        实测（跑生产打分函数现算）：零证据地板 0.2015，事实类查询 ×1.35 后
        0.2720，高信任高热度 0.4000，叠加 0.5400，再叠 ignition ×1.5 到
        **0.8100 —— 越过「真相关」参照的 0.6065**。**这推翻了「一道总分门槛
        就够」的前提。** 修法两层，都落在 scoring.py 单一出口：证据闸门
        （双零出局，默认开，承重）+ 复合总分门槛（默认 0.0，先观测）。
        ignited 条目豁免总分门槛 —— recall_funnel.py 在打分返回**之后**才乘
        IGNITION_BOOST=1.5，门槛看到的是 boost 前的分。
    19. 复核推翻上游材料五处：权重不是仓里的权重（单字误命中一项方向反了）·
        漏了 1.35× 事实类增益（0.3 只剩 0.028 余量）· 「全仓无闸门」不成立 ·
        **漏看了 hot/search.py 已有的 AIDUMEM_RECALL_SCORE_FLOOR**（向量分轴、
        默认 0.0、带遥测，理由与我独立想到的一字不差 —— 那是 v20.1 的裁决）·
        CJK 修法前提错（分词器切整段 CJK 串，不产生单字 token；真机制是
        `tok in text` 子串匹配，照那样改会让单字中文查询 BM25 整条腿失明）。
        CJK 移出范围另立项。新变量用 AIDUMEI_ 前缀（品牌守卫抓到）。
    20. **实机实测把方向又改了一次**：证据闸门在**活库上几乎不触发** ——
        向量检索对任何候选都给大于零的相似度，vec=0 只在向量腿降级时出现。
        **单元测试里的「零证据」是生产上不存在的情形，我自己造了个假绿灯**，
        靠实机测试才抓出来。实测：查询「复盘召回质量」→ 真相关 0.7165 /
        无关 0.4062 / 0.3870；问一个毫不相干的问题 → 三条全无关
        0.2862 / 0.2819 / 0.2362。真矛盾在别处：部署方早就用
        AIDUMEI_RECALL_VERDICT_THRESHOLD 声明「低于这个分不可信」（生产配
        0.46，真实分布标定），系统也照此判 not_found，**却把同一批结果原样
        返回**。最后一刀：hot/search.py 的召回下限未显式配置时**回落到那个
        已标定的阈值**；显式设 0 可回旧行为。反转了 test_v20_1_recall_verdict
        里「verdict 不丢数据」那条契约，用例改名并写清原委 + 验逃生门。
    21. 反转既有契约：test_v20_1_recall_verdict 那条「verdict 是判语不是过滤器，
        结果一条不许丢」被反转 —— 原设计讲得通，但它造出了「我知道这批不靠谱／
        我照样给你」同时成立的输出。用例改名、写清原委，并加一段验逃生门
        （显式关掉下限后旧行为必须完整回来），否则这次反转不可回退。
    22. 14 条守卫 + 六条变异探针（摘证据闸门 / 闸门漏 bm25 那一半 / 摘 ignited
        豁免 / 开关非法值改 fail-open / 门槛默认改 0.3 / 删交叉引用）**全部验红
        后还原**。其中一条盯着「三道闸门的交叉引用不许被删」—— 防止下一个人
        把不同轴的闸门当重复实现合并掉。
    23. 参赛前全面自查（仍不升版本号）：四条测试依赖「重排不可达」——
        沙箱绿、部署机红，唯一变量是重排服务可不可达。修法三件套：默认摘掉
        重排 + 用可控替身把重排打开另验融合后逻辑 + 元守卫（凡断言打分输出的
        用例必须声明重排状态），守卫当场抓出一个存量用例。
    24. 宣称证伪推翻三条：.env.example 自称完整却差 13 个真在读的键（含
        是否允许无凭据监听公网、注入防御档位）；中英两份 README 变量表不一致
        （英文缺 API_TOKEN——只读英文的人不会知道鉴权开关存在）；样例里的
        AIDUMEM_HOST_MEMORY_MD 全仓无人读。全部修掉，配双向守卫。
    25. 零配置首跑：/add 从 500+内部异常原文改为 503+可操作指引；degraded_details
        与 degraded 同源（此前恒 None）；29 处裸 str(e) 5xx 收敛到 api_errors.py
        单一 helper。
    26. 收编生产机上未提交的 health_check.py 幽灵端口修复（实测无进程监听），
        三方同 SHA 恢复。环境矩阵进 README：五种环境实测（1487/12 · 1497/2 ·
        1497/2 · 1496/3 · 1499/0），每行 通过+跳过=1499 且跳过逐条归因；
        无归因的差异=还藏着一条「换环境才现形」的缺陷。
    27. 10 条守卫 10 条探针全部验红后还原；环境⑤ 有过一次不可复现的
        5 failed + 1 error（与 pip 安装共用网络窗口，后续三次连续全绿），
        记为已知风险。
    28. 证伪工具自身造过两次假发现（射程太窄误报两个变量；lstrip("./")
        逐字符剥把 .env.example 剥成 env.example）—— **证伪工具本身也需要
        负向对照**，会造假发现的自查和会漏发现的自查同样危险。
    29. 10 条新守卫、10 条变异探针全部验红后还原。环境⑤ 有过一次不可复现的
        5 failed + 1 error（与 pip 安装共用网络窗口，后续三次连续全绿），
        如实记为已知风险。
    30. **单条删除三态是实机部署后补修的第三笔账**：沙箱全绿、四道关全过，
        不代表出口契约真的到达调用方；生产冒烟抓出 raw 句柄删不掉、单条
        /delete 仍硬编码 ok、raw 的 facts 行漏删。改成句柄换算统一函数 +
        按同域枚举反查 + 模块级失败登记器；not_found 判据被 delete_local
        的假计数拆穿后，改为先 retrieve 核实存在性。9 条守卫含 P5 句柄
        形态扫描，五条变异探针全部验红后还原。用例总数 1499 → 1552。

v20.2.4 (差异化时效衰减 + 纠正语登记 · 2026-08-27)
    主题：借鉴一个同源分支的两样东西——差异化时效衰减与纠正语检测，
    **借参数不借表结构**。公开 Tag/Release 停在 v20.2，版本号仅服务侧
    三段式推进（SOP 双轨）。
    1. WP-A 差异化时效衰减（ducky/scoring.py）：六型各自的衰减率
       （PREFERENCES 0.00 / DECISIONS 0.02 / FACTS 0.05 / REFLECTIONS 0.08 /
       EXPERIENCES 0.16 / OBSERVATIONS 0.28）替代此前全局单一 λ。
       **零新表、零新查询、零额外往返**——类型账本 v19.0 就有，且
       score_and_rank_candidates 早已在循环外批量查好（注释写着「彻底
       消除 N+1」）。FACTS 取 0.05 等于旧全局默认，而上游类型缺失时回退
       字面量 "FACTS"，于是未分类的存量记忆**行为逐字节不变**。
       开关 AIDUMEI_TYPE_DECAY **默认关**。
    2. 实测区分窗口 7~30 天（跨度 0.86~1.00）；90 天后除 PREFERENCES 外
       全部饱和到 0。最大绝对扰动 = 时间权重 0.15，与既有 1.35x 六型增益
       同量级，撼不动 vector（0.35）。**这两个数是实测的；六个 λ 是工程
       惯例值，与召回阈值 0.46、熔断 N/M/T 同款待生产分布校准。**
    3. WP-B 纠正语登记（ducky/conflict_resolver.py）：纯谓词 is_correction()
       **只登记不判决**。原计划给 resolve_fact_conflict 加「置信度提升」，
       落点**不存在**——两个入口都是确定性判决（同 key 不同 value 直接
       软失效），没有可加分的灰度。改为：无消解时只落日志（不为记账而
       打库，保住原有的快速返回路径），有消解时把标记附在**已经要写的**
       那条账本行上。属性级入口不接此信号（原文不在它手上）。
    4. 红线守卫判据精化：从「调用 is_correction 的函数不得含写库 SQL」
       改为「纠正语的返回值不得把守写库分支」——粗判据会把合法的标注
       用法也判红，而逼人绕过守卫比没有守卫更糟。守卫自带射程自证
       （人造违规样本必被抓）与**合法样本负向对照**（合法标注必被放行）。
    5. 收益面如实标注：类型账本 344 行 / 云端向量库 1635 点 = 覆盖 21.0%，
       其中非 FACTS 104 条 = **本特性真实收益面 6.4%**。现有
       backfill_from_facts **提升不了它**：该工具写 "fact:{整数id}" 键，
       检索路径查的是 36 位 UUID —— **两个键空间永不相交**。给存量
       1291 条补 UUID 键登记的工具尚不存在，登记为独立议题。
    6. 测试：新增 1 个点名文件 50 条用例
       （tests/test_v20_2_4_type_decay_and_correction.py）。计划书原写的
       一次性变异探针**焊成了常驻守卫**（投毒表 / 压平表 / 剪断接线的
       前提反证），不靠下一个人记得手工验。3 处变异探针逐一验红后还原。
       用例总数 1367 → 1442。

    7. **第三方安全复审（马院士，评级 C·有条件不通过）：27 项逐条核验零误报，
       全部认账。** 做法不是「改 27 个地方」，是把 27 个症状收敛到 5 条原则上，
       每条配一个结构守卫：① 单一真相源不靠调用点自觉 ② 补能力不改措辞
       ③ fail-closed 是默认 ④ 边界靠编码不靠检测 ⑤ 守卫能抓住下一个同类。
    8. 入口（F-01）：公网门禁此前只长在 main() 里，而 uvicorn api_server:app /
       gunicorn 不经过它 —— _lifespan 的 docstring 讲的正是这条路，v20 的 P1-6
       修了后台能力却漏了安全门禁。三道保险：argv 探测（`uvicorn --host` 是
       **别人的**命令行参数，环境变量看不到）+ lifespan 拒启 + 无凭据实例
       **只服务回环**。
    9. 隐私（F-03）：local 档云出口改在**最底层**阻断（call_llm / rerank /
       Vision）。此前档位谓词只接在 add/search 主链上，九个模块直接调 call_llm，
       而双语 README 写着「零 token、零外部网络」——**那是假宣称**。
       Vision 那处是门槛测试当场抓出来的漏项（我按顶层目录 grep 时漏了
       ducky/pipeline/ 这一层）。
    10. 域隔离（F-04/05/06/07/08/09/11/15/17）：ducky/extended/routes.py 这批
       次级事实端点在 v20.0 的全量域隔离里**整体漏了**（UPDATE facts … WHERE
       id=? 零 scope、SELECT fact_value 全库返回正文）；治理审批只比 bank 不比
       user；coalesce 键缺 bank 且回调靠闭包捕获 scope。统一改用
       ducky/facts_recall.py 的 tenant_clause 作唯一 scope 谓词，batch 自带
       **不可变** scope，all_scopes 从 HTTP 契约整个移除。
    11. 注入边界（F-12）：改成**编码** —— nonce 化闭合标记 + 正文边界中和。
       此前 wrap_inject_frame 只要在正文里看到 `<memory>` 就认为「已包装」
       直接返回，**一道能被它保护的内容自己关掉的防御**；沙箱 delimiter 零转义。
    12. fail-closed（F-13/14/18）：ducky/conflict_resolver.py 的 attr_re 三处
       解包全被丢掉，通用状态词成了域内广谱杀虫剂（实测「请关闭通知」让两条
       无关事实同时失效）；严格租户判定任何异常一律放行；GUARD_MODE 拼错
       静默降级为 log-only。三条都改成「不确定就从严」。
    13. 本版自己的缺陷 + 新守卫（F-15/19/20 + 自加）：类型分档在命名 bank 下
       完全失效（偏好类时效分 1.0000→0.0111，被当 FACTS 打折 90 倍）；登录表
       重写后 10,000 IP 从 0.433s/无界变成 0.011s/4096 条硬上限；A-1 修了配置面
       的 nan 却漏了**数据面**（exclusive_minimum 那个能力 A-2 就加好了、没用在
       那一行）。新守卫：测试替身签名**逐参数对齐**生产签名 —— F-15 躲过 50 条
       用例就是因为替身 `lambda ids: types` 比生产少两个参数，守卫上线
       **一次抓出 8 处**同类隐患。其余 F-02/22/23/24/25/26/27 一并落地。
    14. F-21 逐工具对表（tests/test_v20_2_4_mcp_contract.py，静态 AST × FastAPI
       真实路由表，不起服务因此能进主套件）当场抓出**两个真错位**：facts_add 把
       content 发进 JSON body，而端点入参是散装标量且**根本没有 content 字段**
       —— 该工具从来没成功存进过任何一条事实（请求 200、内容全丢）；session_start
       发的 session_id/metadata 端点一个都不接收（外审未点名，本轮对表新发现）。
       两处已修，_api_post 补 query 支持。
    15. F-26 真构建验证：package-data 的 `../frontend/**/*` 写法**确实生效**
       （wheel 内 frontend 16 / integrations 11 / deploy 3 / 两个样例齐备）。
       我原以为 setuptools 不支持跳出包目录 —— **不验就不知道**，无效配置
       会以「已修」的名义发货。声明层加静态守卫，真构建放 CI。
    16. F-27 三个验收 job（mcp-extra-import「装得上≠用得了」/ wheel-assets /
       docker-context-secrets「造假密钥让 docker 自己说话」），各带前提反证；
       触发方式保持维护者裁决的只手动，且有守卫盯着这条裁决不被悄悄改掉。
       顺带修了 CI 守卫自身：shlex.split 遇行尾续行（合法 shell）抛 ValueError
       —— 守卫崩溃是 ERROR 而非 FAIL，读起来像「守卫坏了」而不是「代码有问题」。
    17. 测试卫生（清理战场时发现的根因）：生产机 /tmp 攒了 3573 个 aidumem_*
       临时目录 146MB（08-24→08-28）—— tests/ 里 45 处 mkdtemp 绝大多数不注册
       清理，而 pytest 的 tmp_path 本来自带「保留最近 3 次」，mkdtemp 绕过了它。
       conftest.py 加会话级兜底回收（单一前缀 + 系统 tmp 内 + realpath 复核 +
       失败静默），位点登记守卫提醒优先用 tmp_path。**快照必须落在
       pytest_configure**：模块顶层的 mkdtemp 发生在 collection 期，而 session
       fixture 的 setup 在那之后，拿到的 before 已包含它们 —— 实测症状是
       「跑完一轮稳定剩 1 个」。

v20.2.3 (外部审计整改 · 2026-08-27)
    主题：第三方独立审计（完全外部视角：从公开仓克隆、独立 venv 复现）
    的 1 高危 + 3 中危 + 4 低危逐条复核整改。公开 Tag/Release 停在 v20.2，
    版本号仅服务侧三段式推进（SOP 双轨）。
    1. H-1 入门路径依赖补齐：requirements.txt 补 python-multipart —— 它在
       pyproject 声明着、本清单却漏了，而 FastAPI 的 Form(...) 在**路由
       注册期**就要它（ducky/hot/legacy_routes.py、ducky/extended/routes.py
       各一处）。CI 与 Dockerfile 都在其后补跑 pip install .，恰好遮蔽了
       缺口 —— **唯一裸奔的是 README「30 秒上手」教新用户走的那条**。
       干净 venv 实测复现：RuntimeError: Form data requires
       "python-multipart"。与 v19.4.2 的 inotify_simple 同类事故，本轮焊成
       守卫（tests/test_v20_runtime_deps_declaration.py：pyproject 运行时
       依赖 ⊆ requirements，PEP 503 名称规范化，例外须显式登记）+ CI
       新增 bare-requirements-smoke job（含「本 job 未装包本体」的前提反证）。
    2. M-2 配置雷全仓拆除：新增 ducky/env_config.py 作单一真相源
       （叶子模块，只 import os/logging —— 配置解析被 auth 这类底层模块
       在 import 期调用，任何内部依赖都可能织出循环导入）。审计点名 2 处，
       自查普查出 6 处，元守卫上岗后又抓出 2 处：ducky/security/auth.py
       （SESSION_TTL，**炸在 import 期=服务起不来**）、ducky/scoring.py 三处、
       ducky/security/injection_guard.py、api_server.py 端口、
       scripts/dev_server.py、integrations/cursor-hook（后两者是独立脚本，
       就地安全解析，语义一致）；ducky/gear.py 与 ducky/rate_guard.py 的
       v20.2.1 本地实现收编进单一真相源，公开行为逐字不变。元守卫判据走
       **AST 不走字符串**——正则版把 env_config 自己头注里的反面例子判成了违规。
    3. M-1 登录爆破护栏：ducky/rate_guard.py 新增按 IP 的登录失败计数
       （只计失败、不计成功；**先查后验**——超限直接 429 连 PBKDF2 都不跑，
       既省 100ms 也不给攻击者旁路信号），api_server.py /login 接线，
       env AIDUMEI_LOGIN_FAILURES_PER_MIN（默认 10，0=关闭）。反代之后
       退化为全局阈值的局限如实注明：X-Forwarded-For 可伪造，绝不拿来分桶。
    4. L 组：api_server.py 检测到 HTTPS 反代痕迹而 AIDUMEM_COOKIE_SECURE
       未开时告警（与「配置不静默」纪律一致）；ducky/router_usage.py 把
       AIDUMEM_ROUTER_SSH_STRICT=yes 从「可配」升格为生产验收线；
       docs/README_draft.md 草稿移出仓库。
    5. M-3 如实登记不冒充修复：前端 vendored echarts 5.5.0 命中
       CVE-2026-45249（Lines series + 默认 tooltip formatter + data name →
       innerHTML）。独立核查确认**当前不可达**：仓内唯一用法是
       frontend/js/panels.js 的 graph series + 自定义 formatter 且已过
       esc()，全仓无 lines series。升级到 6.x 是跨大版本、须配 UI 实测，
       本版不动，留待专项。
    6. 引擎三档可选：ducky/engine_mode.py —— AIDUMEI_ENGINE_MODE=
       auto|cloud|local。这同时是本轮唯一有效的内存优化：备胎实测常驻
       +151MB（onnxruntime 库 75MB + 模型会话 122MB），四种旋钮调优实测
       全部无效、模型已是最小的中文可用款，**唯一有效的优化就是不加载它**。
       两腿独立谓词接线到 local_embed（省内存闸门）/dual_index/gear/
       ducky/hot/add.py（本地档 action=local_only 且不入云欠账）/health 探针。
    7. 自查 S-1：ducky/rate_guard.py 的计数表从不清理，而 /login 免鉴权 ——
       实测 5 万个源 IP = 5 万条常驻条目（约 12MB），本轮自挖自填；照抄
       ducky/security/auth.py 早就有的清理模式（超阈值才扫、只丢死条目、
       语义无损），新增 window_count() 可观测面。
    8. 自查 S-2：元守卫盲于 import 别名（`import os as _os` 直接溜过），
       加固当天就抓出它原先漏掉的 ducky/hot/legacy_helpers.py 一处 ——
       守卫的盲区比它守的缺陷更危险。
    9. 自查 S-3：欠账水位有数字没判据。ducky/dual_index.py 新增
       pending_verdict() 三态，stuck 的判据是「数字大**且**上次重放无进展」，
       阈值 AIDUMEI_PENDING_WARN_LEVEL 默认 500（惯例值，标注待校准）。
    10. 自查 S-4：README 部署章整章数字过期，且「本机不加载任何大模型权重」
       在自动挡之后已是假话。按 2026-08-27 生产实测重写为双体量对比表，
       并修正 1 核 1G 的口径。宣称即承诺。
    11. A-1（第二轮外审·中危）：ducky/env_config.py 的边界判据被 NaN 旁路
       （NaN 与任何数比较恒为 False → not(False or False) 判为合法），
       静默通过且探针零痕迹 —— 假绿灯长在拆雷模块里；1e999→inf 在无上限
       参数上同样旁路。共用层 _resolve 先拦非有限值（math.isfinite），
       非法值词表补 nan/inf/1e999（旧词表 inf 拦得住而 nan 漏网）。
    12. A-2（低危）：v20.2.1 的 `v > 0` 被 minimum=1e-6 近似收编，(0,1e-6)
       的合法旧值被拒 —— 宣称的「逐字不变」不逐字。float_env 增加
       exclusive_minimum，ducky/gear.py 改用它表达真正的严格大于零。
    13. A-3（低危）：version.py 的用例总数在同一次发布里过期，而本版
       S-4 自查项刚写过「数字过期就是假话」。除改数字外新增三面对账守卫
       （version.py / CHANGELOG.md 宣称值 = pytest 实数，且两者互相一致）。
    14. A-4（低危）：本地档下 gear_status 仍报 full/closed，等于宣称云腿
       正在服役。改报 disabled_by_policy 并保留熔断器内态；**只动探针面**，
       current_mode() 维持 full|lite 二值（ducky/hot/add.py 据其分流），
       ducky/hot/search.py 的 engine_mode 改为先看部署配置。
    15. README 门面双语重构：以三档为主结构（原「两种动力」二档表已与
       v20.2.3 的三档可选不匹配），并把消耗说到底——151MB 去向逐项摊开、
       四种失败的优化尝试逐个列名、备胎常驻的取舍单列。卖点与短板同屏。
    16. 测试：新增 2 个点名文件 22 条用例
       （tests/test_v20_runtime_deps_declaration.py 依赖双清单守卫 +
       tests/test_v20_2_3_audit_remediation.py 配置雷子进程验证与登录护栏），
       5 处变异探针逐一验红后还原。配置雷用例**跑子进程**是刻意的：
       这些常量在 import 期求值，父进程里 monkeypatch env 影响不到它们，
       那样的测试会稳过且证明不了任何事。三档与自查项另有 20 条
       点名用例、7 处变异探针验红。用例总数 1290 → 1367。

v20.2.2 (LLM 蒸馏腿挡位化 · 2026-08-26)
    主题：自动挡补上第三条腿。实弹取证（2026-08-26 冒烟恰逢 LLM 网关
    521 瞬态断供）：嵌入活着而 LLM 死时，mem0 内部 openai 客户端按
    Retry-After:120 盲重试，把单次 /add 同步挂 4.5 分钟——LLM 腿此前
    无挡位裸奔（嵌入腿 v20.2 有挡、rerank 腿 v20.1 已软失败）。
    公开 Tag/Release 停在 v20.2，版本号仅服务侧三段式推进（SOP 双轨）。
    1. 传输层快失败：ducky/mem0_patches.py §6 llm_transport_policy ——
       mem0 内部 openai 客户端 max_retries=0 + 45s/connect 10s 超时
       （45s 对齐 ducky/llm_client.py 已运行多版的验证值）；重试职责
       上移给挡位与降级链。顺序契约：先换客户端实例再包用量追踪，
       反过来 usage_tracking 静默空转。
    2. LLM 蒸馏腿挡位：ducky/gear.py 泛化为参数化 _Breaker 双实例
       （嵌入腿公开 API 签名与行为逐字不变，既有测试零改动全绿）；
       LLM 腿 should_try_llm/record_llm_failure/record_llm_success，
       env AIDUMEI_LLM_GEAR_*（回退语义同 R1），事件账本
       target_id=llm_leg，/health 新增 llm_gear 探针
       （ducky/hot/health.py）；.env.example 补录两腿参数。
    3. 写路径接线：ducky/hot/add.py —— 挡位 open 时跳过 layer1 直接
       确定性直写秒回（infer=False，原文/硬事实/云向量照落可召回），
       响应带 distillation 注记（additive）；LLMError 形态失败上报
       挡位并本请求就地降级；直写内层再撞 LLMError 自纯化（fallback
       不许自己 500——洞③闭合）；非 LLM 故障保持旧语义透传 infer，
       且不污染 LLM 腿信号（Y2 教训写侧版）。
    4. 测试：9 条点名用例（两腿独立/三态防抖/降挡后跳过 layer1/信号
       纯净/半开真实写入升挡/双重故障自纯化/传输补丁两态），6 处变异
       探针逐一验红后还原（tests/test_v20_2_autoshift.py）。用例总数
       1281 → 1290。
    5. 文档：双 README 自动挡门面补 LLM 腿条目与数字同步。

v20.2.1 (自动挡外审整改 · 2026-08-26)
    主题：v20.2 公开后两份独立外审（生产侧敌对复审 + 外部结构性审计）的
    采纳项落地——4 🔴 全修 + 2 🟡 + 残窗闭合。公开 Tag/Release 停在
    v20.2，本版随 main 公开源码，版本号仅服务侧三段式推进（SOP 双轨）。
    1. 拆配置雷（R1）：ducky/gear.py 三阈值与 ducky/rate_guard.py 限流的
       非法 env 由 raise 改为**回退默认 + warning 一次 + 探针常驻**
       （gear_status().config_errors / rate_config_errors()，
       ducky/hot/health.py 探针换口径）——它们站在保命/写路径主干上，
       raise 会把「断供保命」反转成「配置笔误即全站 500」；「非法值不
       静默」纪律不变，出声方式从炸改为可观测。启动参数自检日志随
       启动对账打印（ducky/wal_engine.py）。
    2. 启动重放兜底（R2）：欠账重放此前只挂在升挡事件上，而重启把挡位
       重置回 closed——升挡事件重启后永不再来，lite 期欠账成永久赖账。
       ducky/dual_index.py 新增 spawn_replay_daemon（零欠账不起线程），
       reconcile_startup 收尾兜底重放（两条返回路径共用），ducky/gear.py
       升挡重放收编同一入口；/health 欠账水位旁新增 last_replay。
    3. verbatim 本地点单删闭合（R3）：该类点 id 由 (原文, 域) 派生、
       不与 memory_id 同源，单删钥匙够不着——降挡窗口已删内容会从备胎
       复活。抽出纯函数 verbatim_local_pid（改派生公式=同时改写删两侧），
       ducky/wal_engine.py §8b 搭车 §0a 正文重演派生精确删除；覆盖精度
       与 §6 原文层同级（正文逐字一致才命中），delete_all 按域谓词删
       仍是全量兜底。
    4. 重放防自我复制（R4）：本地欠账重放失败时 upsert_local 内部再入
       新账 + 外层回滚原行 = 每轮净增 1 条。ducky/dual_index.py
       upsert_local 增 enqueue_on_fail 参数，重放路径置 False——失败
       只回滚原行留账下轮。
    5. 残窗闭合（外部审计建议）：claiming 抢占后、mem.add 完成前同租户
       delete_all 交叉的秒级窗口，此前如实登记不冒充零；本版补偿闭合——
       重放 add 完成后复核账本行仍在否，行没了即撤销刚写入的点
       （ducky/dual_index.py _revoke_replayed_add：删除意愿 > 补算完整性）。
    6. 备胎禁网强制化 + 熔断信号提纯（Y1/Y2）：ducky/local_embed.py
       HF_HUB_OFFLINE 由 setdefault 改强制覆写（外部预置 0 不可再绕过
       禁网纪律）；ducky/engine.py 外层 except 不再记云失败——云调用已
       被内层 try 精确包住，复筛/装配等非云腿异常误记会让半开探测明明
       成功却被装配 bug 打回 open，云「永远恢复不了」。
    7. 测试：新增 11 条点名用例（重启还账端到端/单删够到备胎点/持续
       故障欠账恒定/残窗撤销/装配异常不动熔断，均带区分力对照），两条
       「非法 env 必抛」旧断言翻转为回退语义（tests/test_v20_2_autoshift.py
       与 tests/test_v20_1_1_scope_hardening.py），5 处变异探针逐一验红；
       logger 钉子 90→91（tests/test_v20_brand_policy.py，rate_guard 新增
       告警 logger）。用例总数 1270 → 1281。
    8. 文档：双 README 测试数字同步；lite 挡 add 响应（deferred_distillation）
       字段形态注记——该分支无判语字段族，属写路径契约而非召回契约。

v20.2.0 (智慧引擎自动挡 · 2026-08-26 正式发布)
    主题：外部服务失效时自动降挡无感续跑，恢复时自动升挡欠账回补，挡位
    永远诚实可见。「V20 就是双引擎、自动挡、市面独一份」（维护者定调）。
    开发期以私有验证线 20.2.0-dev.N 迭代；断供演练两轮实机验证、验收基石
    逐条对表、自审补位（无外审轮）后，经维护者授权公开（Tag v20.2）。
    1. WP-E 本地嵌入备胎：ducky/local_embed.py（fastembed/ONNX，
       BAAI/bge-small-zh-v1.5 512 维）——阶段 0 双环境 POC 定案（sanity
       双 6/6，单条 1.0ms 开发机 / 6.7ms 生产 2 核）；运行时强制离线
       （HF_HUB_OFFLINE），模型由 scripts/fetch_local_embed_model.py
       部署期就位（支持 --from 离线拷入）；fastembed 进可选依赖组并登记
       第十一条跳过轴。
    2. WP-F 双索引与欠账：ducky/dual_index.py —— 本地 collection
       mem0_local（512 维）与云库同源 id；原文本地向量在 /add 路由层
       单点写入（ducky/hot/add.py，lite 挡语料）；核心块双写
       （ducky/core_memory.py）；欠账账本 pending_embeddings（lite 挡
       整笔蒸馏欠账 + 本地单点欠账，恢复重放）；删除链新增本地腿与
       欠账腿（ducky/wal_engine.py §14/§15 + 单删 §8，矩阵两条新裁决）；
       存量回填 scripts/backfill_local_vectors.py（dry-run 默认，生产
       执行停点）。
    3. WP-G 熔断切换器：ducky/gear.py 三态机（N=3/M=2/T=60s 惯例值，
       标注待生产故障分布校准）；半开态拿真实流量当探针（首跑演练抓出
       的死锁：不试探则成功信号永不来，系统卡死备胎挡）；云腿失败时
       ducky/engine.py **同一请求内**落本地腿兜底——无感顺滑到单次查询；
       升降挡进事件账本，升挡触发后台欠账重放。
    4. WP-H 挡位诚实化：/search 响应带 engine_mode（按本次实际用的腿，
       ducky/hot/search.py），lite 挡附 confidence_scale 口径注记；
       /health 挡位/熔断内态/备胎在场/欠账水位/本地点数五探针。
    5. 测试：新增 13 条点名用例（tests/test_v20_2_autoshift.py，含断供
       演练全链端到端、假恢复防抖、降挡期云索引零写入负向）；用例总数
       1267；双 README 与轴表同步。

v20.1.1 (公开后外审加固 · 2026-08-26)
    主题：v20.1 公开发布后两份独立外审（生产侧复审 + 社区结构性审计）的
    复核采纳项落地。公开 Tag/Release 停在 v20.1，本版随 main 公开源码，
    版本号仅服务侧三段式推进（SOP 双轨）。
    1. 限流护栏：新增 ducky/rate_guard.py（进程内固定窗口，按租户分桶），
       ducky/hot/add.py 写路径默认 120/min、ducky/hot/crud.py delete_all
       默认 3/min —— 默认值取自生产 14 天日志实测（分钟峰值 35 → 3.4 倍
       余量），拦失控循环不拦正常流量；429 带 Retry-After；
       AIDUMEI_RATE_ADD_PER_MIN / AIDUMEI_RATE_DELETE_ALL_PER_MIN 可调
       （0=关闭，非法值报错点名）；生效值进 /health（ducky/hot/health.py）。
    2. metadata 形态白名单：ducky/api_models.py 的 AddRequest.metadata
       校验键名形态（中英数与 .-_，1-64 字符）、键数 ≤32、单值 ≤4KB、
       总载荷 ≤16KB、嵌套深度 ≤2 —— 挡注入炸库，不伤正常键（含中文键）；
       顶层 extra="allow" 的兼容语义保持不变。
    3. R-18 删除链补齐：ducky/wal_engine.py cascade_delete_all 新增 §12
       observations（user 轴——表无 bank 列，user 轴是其全部表达力；v7
       无主存量行不动）与 §13 scenes（全轴谓词）；DELETE_CHAIN_MATRIX
       两表改判 clean，persona 豁免理由经侦察改判为「租户轴正交」
       （persona_key 与租户模型正交，作用域删除语义不成立）。
    4. 源码守卫三连（tests/test_v20_1_1_source_guards.py）：前端
       innerHTML 拼接守卫（未审计表达式直拼即红——外审 XSS 指控虽经
       58 处全量审计驳回，但「安全靠人工纪律」是真的，守卫使其结构化）；
       f-string SQL 插值登记（65 处基线人工核对，新插值不登记即红）；
       schema 迁移点总账（60 位点，additive-only 纪律配上全景账）。
    5. 行为测试 22 条（tests/test_v20_1_1_scope_hardening.py 与守卫文件，
       含跨租户负向、v7 存量行保全、变异探针双向验证）；用例总数 1254。

v20.1.0 (确定性兜底与诚实召回 · 2026-08-26 正式发布)
    主题：LLM 不在场时，记忆系统仍然是完整的记忆系统；召回给不出可信结果时，
    宁可诚实说「没有」。开发期以私有验证线 20.1.0-dev.N 迭代；五份外部评审
    收敛的 R-01~R-17 整改全部闭合后，经维护者授权于 2026-08-26 公开
    （Tag v20.1）。预案与验收基准存内部评审文档。
    1. WP-A 确定性抽取层：ducky/pattern_extract.py 七类规则抽取（日期/版本/
       数字/链接/键值/指令/偏好）挂 /add 路由层（ducky/hot/add.py），产物
       source='pattern_extract' 落 facts 层 —— LLM 空抽取时硬事实不再丢失。
       零 token、确定性、可按来源精确清除；开关 AIDUMEI_PATTERN_EXTRACT。
    2. WP-B 无 LLM 整合升级：refine_memory 三档降级链 llm → extractive → rule，
       提取式档保留具名实体与数字的要点清单；consolidation_basis 记账
       （additive 迁移）；facts 水位阈值配置化（AIDUMEI_FACTS_WATERMARK）。
    3. WP-C 召回弃答信号：ducky/hot/search.py 响应带 recall_verdict 三态
       （found / not_found / degraded），故障先于缺失 —— ducky/engine.py 向量腿断
       产生的空结果判 degraded，绝不冒充「查无此忆」；召回腿遥测随响应下发；
       置信下限 AIDUMEI_RECALL_VERDICT_THRESHOLD（默认 0.0，校准属部署决策）。
    4. WP-D CoreMemory 还账：D1 ducky/core_memory.py 新写/更新的块进向量召回池
       （payload 对齐 mem0 装配契约，reliability=1.0，稳定点位 id）；存量回填
       scripts/backfill_core_vectors.py dry-run 默认，生产执行须单独停点。
       D2 陈旧告警按块分级（画像/决策 180 · 项目 30，依据联邦分层 TTL 语义），
       env 可覆盖，生效值经 /health 可查 —— 分级是给依据，不是调大消音。
    5. 守卫登记：新 env 全用 AIDUMEI_ 前缀；pattern_extract /
       core_memory_vector_index 进特性账本清单；logger 处数 86→87；README
       双语用例数同步 1200；test.yml 触发面守卫对齐噪音治理决策。
    6. 测试：新增 4 个点名文件共 88 条用例（全含负向对照）；
       test_v20_core_memory_staleness 边界用例随分级契约更新。
    7. 外审收口整改轮 R-01~R-17：ducky/wal_engine.py 删除链补清 workspace /
       core_memory / refined_memories / tombstones / candidate_facts 五本账，
       DELETE_CHAIN_MATRIX 覆盖矩阵元守卫让漏账本结构性变红；
       ducky/pipeline/memory_workspace.py 新增 ws_evict 单条驱逐；
       ducky/pattern_extract.py 对抗样本护栏（键内整词连词/复合单位/URL 遮蔽）
       与按重要性截断；ducky/refine_memory.py 整点丢弃出声 + LLM 降级 WARNING；
       ducky/hot/search.py workspace 命中分支三态字段补齐；ducky/hot/health.py
       探针故障显式 unknown + 阈值校准提示 + 三副本对账入口
       （ducky/core_memory.py audit_core_replicas）；ducky/mem0_runtime.py
       沙箱内拒连沙箱外向量库；ducky/resource_probe.py 跨平台守卫式导入；
       tests/test_v20_mem0_patch_layer.py 缺 mem0 改跳过（第十条跳过轴）；
       新增 32 条点名用例（tests/test_v20_1_delete_chain_closure.py +
       tests/test_v20_1_audit_remediation.py），用例总数 1232。

v20.0.1 (私有预发布 · mem0ai 2.0.19 兼容与删除链收口 · 2026-08-25)
    这是 v20.0 的私有补丁迭代，不代表公开 Tag/Release/PyPI 已更新。
    1. mem0ai 2.0.19 兼容层：上游已修复 list content 时保留原生语义，Role Drop 继续由本地补丁处理。
       涉及 ducky/mem0_patches.py、pyproject.toml、requirements.txt、tests/test_v20_mem0_patch_layer.py。
    2. delete_all 补齐 memory_types 作用域清理，避免 infer=False 写入后留下孤儿类型账本；加入跨域回归测试。
       涉及 ducky/wal_engine.py、tests/test_v20_delete_all_and_wal_replay.py。

v20.0 (全量记忆域隔离 · 可复现评测 · 后端契约与数据生命线 · 2026-08-20)
    核心主题: 把 aiduMEI 从单一隐式记忆池升级为可审计、可复现、可回退的智慧引擎。
    定性: **架构版**。版本号只使用两段式 `20.0`；当前运行时不再依赖神话代号，
    v19 的 Athena 仅作为历史谱系保留。
    1. ducky/bank_contract.py 与 ducky/schema_bootstrap.py 新增显式 user_id + bank_id 契约，
       采用 additive migration，默认 bank 保持存量兼容，禁止以用户输入拼接表名或 SQL。
    2. facts、memory_types、CoreMemory 与冲突消解路径补齐 bank scope，写/查/删/恢复/统计
       使用精确作用域；跨 bank 的结果、覆盖和删除必须被拒绝并可观测。
    3. benchmarks/ 固定 LongMemEval/LoCoMo 数据、模型、judge、prompt、seed、哈希和 JSONL
       协议，适配器执行真实 HTTP 契约；oracle 只作检索上限诊断，不进入 headline。
    4. ducky/vector_backend.py 提供 Qdrant 默认适配器与 sqlite-vec 可选 POC，先影子比对与回退演练，
       不把不可逆换库与数据域迁移绑定；影子迁移与五重平价校验落地为 scripts/vector_shadow_poc.py
       （源库只读白名单锁死、.lock 拒开工、检查点续跑），实测与决策记录进
       docs/ADR-001-vector-backend-contract-and-poc.md，开关模板进 .env.example；
       组件故障进入 degraded/trace，不能伪装成空结果。
    5. /health、/metrics、/search trace 与后台任务补齐 bank/backend/降级证据，配置 reload
       会清理 rerank 缓存并重新报告实际 provider、错误和耗时。
    6. 本地沙箱、小仓 dogfooding、数据集合快照和发布卫生门禁形成 v20.0 G0-G8 验收链；
       未完成评测与数据生命线证明前，不宣称分数、不切默认后端、不推大仓或发版。
    7. 全库读路径泄漏清剿: governance/autodream/scenes/reflect/salience/opinions/obsidian/
       broadcast/session/联邦 recall/persistence/v8 五脉/事件账本/evolve 反馈/毕业链/persona
       逐条补齐作用域，统一「命名域下推+复筛、默认域保持 v19 形状但复筛」；
       非法作用域取数前即抛，错误消息不泄露存在性。
    8. 六处管理员聚合面（联邦 registry/broadcast/tiers、salience 指标与审计、evolve 全表
       演化周期）逐一核对后刻意保留全库统计: 只输出内容无关的计数，属运维视角而非
       用户数据视角。此为有意决策，记录在案。
    9. 可复现性闸门 G3 拆为 G3a（生产同路 infer=true，只断言结构不变量、允许 digest 漂移）
       与 G3b（--deterministic infer=false，断言 digest bit 相同）: 原 G3「含远程模型仍逐
       字节一致」原理上不可达，永红的闸门等于没有闸门。gate 与写入通路一并进 digest，
       互相冒充硬拦；服务端不回显 infer:false 时适配器直接抛错，/add 把 infer 提升为
       公开回显字段——未经证实的确定性一律不接受。
    10. 修掉一次假绿: LoCoMo 的 dia_id 从未灌进元数据，证据匹配器拿它去检索结果里找，
        evidence_hits 结构性恒空、召回诊断恒 0.0，两遍 digest 相等只是空对空的一致
        （run B 吞掉 3 次超时 + 3 次重试仍未扰动 digest）。闸门补实质性检查: 零记录 /
        全部 retrieved_count=0 / 适用题命中全空判红，失败分类一个都不许漂；部分空只
        警告不判红——空结果是协议承认的合法结果，拒答题的空更是正确行为。
    11. 模型派生浮点（score/_hybrid_score/_time_decay）不进 digest，改为显式容差 5e-3
        另查并报出实测噪声地板（|Δ|=0 也必须报出，「没报」与「没量」须能区分）:
        实测 |Δ| ≤ 7.931e-04(LoCoMo 24 值) / 2.411e-04(LongMemEval 12 值)，
        即便模型出环 embedding 仍是远程服务、浮点仍抖。不用四舍五入是因为 ~6e-4 会在
        第 3 位分桶边界翻面，造出比没闸门更坏的间歇性红灯。成员、顺序、正文、
        离散排名 _bm25_rank 照旧进 digest（剥数值不等于变成无序集合）。
    12. 新增 benchmarks/compare_runs.py 把闸门变成退出码（0 通过 / 1 判红 / 2 用法错）；
        LoCoMo 记录补上 §5 一直承诺却从未写出的 retrieved_evidence_only（假绿的载体）；
        证据匹配器改认元数据回指与原文精确回指两种判据并逐条留痕 evidence_hit_basis，
        删掉「证据 id 出现在整条记录 JSON 里就算命中」的子串兜底——改写、翻译、摘要
        一律不匹配，这是身份判定不是语义给分。
    13. 拒答题记 N/A 而不是 0.0: 协议第 4 条要求拒答题上「不答」才是正确行为，
        把它按 0 分并入平均等于用正确行为拉低自己的成绩，读数的人还会误判成检索坏了。
        抽象弃答单列计数，不进分母。
    14. 同期查出一处真问题但刻意不夹带修复: 生产同路下 LoCoMo 有一题的正确轮次确实
        被检索到却判 0.0，根因是 verbatim 召回路径回来的对象没有 metadata 字典
        （verbatim_turns 表无 metadata 列）。这是假红，会让人去修一个没坏的东西。
        本次只在测量侧修（原文精确回指 + 判据留痕，召回诊断 0.0 → 0.5）；加列属于生产库
        结构迁移需单独点头。该路径无元数据级溯源、改写类命中必然漏计，作为已知局限
        如实登记在 benchmarks/PROTOCOL.md §8。
    15. scripts/release_scan.py 第二次被实战打脸: 本版真扫变更面时目标里带了 .md 文件，
        报告打印「已扫 0 个文件 …… ✅ 无硬敏感命中」—— scan_tree 只对目录 rglob，
        喂它一个文件时迭代器为空。两处修: ① 支持单文件目标（带正向对照，单文件通路
        必须能真的报出已知命中）；② 任何目标扫到 0 个文件一律拒绝运行（退出码 2）。
        第二条是要点: v19.5.0 那两道防线各堵一个已知病因（拼错的选项、不存在的路径），
        而两次翻车的症状是同一个 0。判据从病因挪到症状，才不用等下次踩坑再补第四道。
        守卫见 tests/test_release_hygiene.py，其一刻意把敏感词塞进二进制文件里 ——
        词就在那儿，只是没人读过它。
    16. 降级纪律: 兼容降级不许接住真故障。全仓「先按 (user_id, bank_id) 过滤、失败退回
        老口径」的写法原先一律用 except Exception 接 —— 库锁/磁盘满/连接回收都会命中
        同一个降级分支，域过滤被悄悄摘掉，返回值形状与正常查询逐字相同。租户隔离要是
        能被一次瞬时故障摘掉，它就不叫隔离。ducky/bank_contract.py 新增
        is_legacy_schema_error()（只认 OperationalError 且消息确实说了缺列/缺表），
        ducky/reflect.py 取材与 ducky/salience/conflict.py 冲突检测据此改为「不是缺列
        就原样抛出」；reflect 还拆掉外层那个把内层再抛吞掉的 except Exception ——
        否则「库被锁」与「本来就没有事实」同归 {"status":"ok","saved":0}。
        table_columns()（原 _table_columns）与 _table_names() 补 WARNING 留痕: 实测
        PRAGMA table_info(不存在的表) 返回空集不抛异常，那个 handler 只可能被真故障
        走到。ducky/conflict_resolver.py 与 ducky/tombstone.py 手写的 PRAGMA + 宽捕获
        收归 table_columns。新增 tests/test_v20_fallback_discipline.py 10 条守卫，
        逐条做过反证（撤掉修复后应红的 5 条全红且红在预期那一行，恢复后 diff 指纹
        逐字节复原）—— 前后都绿的守卫不叫闸门。
    17. 另外三处宽捕获按病因收窄: ducky/governance.py 的 _row_scope 只认 row 不支持
        按键取值（AttributeError/IndexError/KeyError），否则具名域候选会被静默判成
        默认域；ducky/hot/add.py 的 messages 解析只认 ValueError，不再让「进程出事」
        伪装成「这串文本不是 JSON」；ducky/scoring.py 的 rerank 遥测回写照旧不抛但补
        debug 留痕 —— 本版加那段就是为了修 rerank_applied 看不见，回写自己再静默失败
        则症状与修复前一致。
    18. /update 漏注册记忆域，写路径最后一个缺口: 它会把 bank_id 盖进向量 metadata 并
        按该域重建 FTS，却没调 ensure_bank_registered（add/tombstone/core_memory/
        conflict_resolver 都调了），于是数据落在某域、memory_banks 里查不到这个域 ——
        域存在与否取决于当初从哪个端点进来。ducky/hot/crud.py 补 INSERT OR IGNORE 式
        幂等注册。同期如实登记: memory_banks 被 11 处写入却只有 bank_contract.list_banks
        一个读者，而它全仓零调用方、无端点、无测试；新守卫是它的第一个读者。
    19. layer1 去重更新失败必须留痕（第三个出口，此前无用例走到）: 去重命中却 update
        抛异常时 ducky/layer1_selfcheck.py 静默改走新增，库里多出重复记忆而 action="new"
        与正常新增无从区分。语义不变，补 WARNING 与 details.dedup_update_failed；
        守卫进 tests/test_v20_vector_write_stamp.py，连带钉住降级出口同样盖域戳，
        并加反向对照（update 正常时标记不许出现，否则断言恒真等于没测）。
    20. 重构收尾与文档数字复测: 清掉 layer1 的死 json 导入与两个 v20 测试文件里的
        死导入/零调用辅助函数；用例总数 682 → 693（无宿主 681 passed + 12 skipped），
        README.md 与 README_EN.md 的 12 处测试数字按 --collect-only 实测同步 ——
        文档数字由实测反算，写错就红。
    21. 升级入口的备份闸门自 v19.4.0 起从未成功过一次 —— 永远发红的闸门等于没有闸门:
        scripts/pre-upgrade-check.sh 的代码仓备份写的是 cp -a --exclude=...，而 cp 没有
        这个选项（GNU 与 BSD 双双不认）。实测 cp: unrecognized option '--exclude=venv'、
        退出码 1、目标目录一个不生成，于是这一步每跑必 bad、FAIL 恒 ≥1、整脚本恒退 1。
        步骤 2 那道「无已验证备份就不许升级」的硬门禁本身是对的，却被同一份摘要里的
        这条永久红线拖成「反正都是红的」，真要升级的人只能整体绕过 —— 备份纪律名存实亡。
        改用 tar（--exclude 在 GNU tar 与 bsdtar 上都是实装选项），中间包裹写进 TMPDIR，
        避免 AIDUMEM_BACKUP_ROOT 被指到仓内时把归档卷进归档。能活这么久的原因很朴素:
        全仓没有任何一个测试引用过这个脚本。新增 tests/test_v20_upgrade_gate.py 3 条守卫
        （全仓 shell 不许再给 cp 传 --exclude 且空集不算通过、升级入口必须用真支持排除的
        工具且排除项没掉队、以及一条能力探针真建包解包一轮），第三条是前两条的地基 ——
        把 cp 换成另一个名字不证明新名字管用，只断言退出 0 也不够，被排除的目录照样躺在
        解出来的树里的话备份还是那个胖包。反向对照: 撤回修复则前两条转红、第三条照旧绿
        （它不依赖脚本），恢复后脚本 sha256 逐字节复原。用例总数 693 → 696（无宿主
        684 passed + 12 skipped），两份 README 的 12 处数字同步，并改掉守卫射程外的
        两处陈数（「装了宿主时第一条命令其实跑出 423 passed」是好几个版本前的数字）。
    22. frontend 之外的品牌面两个方向都没有守卫 —— 一个值能无声翻转就等于没有守卫:
        整备期间在一台已部署机器上发现三处 aiduMEM 被顺手改成 aiduMEI，两对一错。
        对的两处是真露脸的字（integrations/aidumem-inject.sh 注入进对话的
        [aiduMEM Recall] 前缀、scripts/health_check.py 打印给运维看的那行），
        错的一处是 ducky/hot/health.py 的 service=f"aiduMEM-v" —— 那是机器契约，
        生产监控按 aiduMEM-v* 匹配，改完 /health 返回 aiduMEI-v，告警自那一刻起
        安静失配而服务一切正常。第三处其实早有守卫（v19.4.2 的反向断言，报错文案
        连「它长得像品牌残留、最容易被下一个人顺手改干净」都预判了），守卫是对的，
        只是没起作用 —— 改的是机器上的文件不是仓里的源码，测试压根没跑；门是好的，
        绕过门的办法是不走门。前两处的问题相反: v19.4.2 的用户可见面射程只有
        frontend/**/*.{html,js,css,json} 加 manifest.json，那两处被改成 aiduMEI
        没人报警、v20 改回 aiduMEM 也不会报警。核实无任何下游按这两个字符串匹配后，
        源码侧采纳这两处（改为 aiduMEI）、机器契约三处原样保留，并新增
        tests/test_v20_brand_visible_surface.py 6 条把两侧一并钉住，同时把「哪些改、
        哪些刻意不改」写在同一屏里，下一个拿 sed 的人不必先猜。空表/文件搬家会让
        逐条断言变成空转，故另有一条断言表非空且所指文件都在。反向对照双向成立:
        露脸两处改回旧名则那两条转红，机器契约改成新名则本文件与 v19.4.2 的老守卫
        一起转红，恢复后三个文件 sha256 逐字节复原。用例总数 696 → 702（无宿主
        690 passed + 12 skipped），两份 README 的 12 处数字同步。
    23. 品牌 VI 统一 —— 露脸的字改，机器认的键一个都不动，历史只增不改:
        边界是「只改品牌 UI，不动系统内的文件夹，让新老客户部署起来无风险、无差异感」。
        全仓 330 处旧名先分类再动手，判据只有一句 —— 这串字符会不会被人读到。
        运行时露脸的（api_server.py 启动横幅、mcp_server.py 工具说明、mem0_sync.py
        的 --help、deploy/aidumem-api.service 的 Description、scripts/update_crontab.sh
        写进用户 crontab 的注释、integrations/hermes-plugin/aidumem/__init__.py 注入进
        宿主对话的那一段）与用户会打开来读/编辑的（ARCHITECTURE.md、.env.example、
        requirements.txt、requirements-dev.txt、mem0_config_local.json.example、
        integrations/INTEGRATION_GUIDE.md、integrations/config.yaml.snippet、
        integrations/cursor-hook/README.md、integrations/cursor-hook/cursor-aidumem.mdc）
        改为 aiduMEI，共 48 处 / 18 个文件。落盘方式是逐行钉住（文件 + 行号 + 原文
        预期）、干跑 48 处全命中才写入，没有用 sed —— 上一条记的那次事故本身就是
        一次顺手的批量替换。机器认的键一处不动: logger 名、/health 的 service 字段、
        线程名、AIDUMEM_* 环境变量、包名/目录名/文件名，外加两处最容易被误认成文案
        的东西 —— ducky/federation/schema.py 的 INSERT OR IGNORE 种子值（为一致而
        冻结: 老装机的库里永远是旧字面量，改源码只会让新老部署从此不一致）、
        scripts/consolidator.py 里那句拿去检索存量记忆的查询串。
    24. 改名带出一个真功能缺口 —— 实体抽取的正则跨不过品牌的两代:
        ducky/hot/legacy_helpers.py 的 _RE_PROJECT 原本只认 aiduMEM，而它是拿去匹配
        存量记忆正文的。re.IGNORECASE 在这里帮不上忙 —— aiduMEM 与 aiduMEI 差在最后
        一个字母。只留新名则改名前写进来的记忆里项目实体全部认不出，只留旧名则改名后
        写进来的全部认不出；两种都不报错，只是安静地少抽出实体。改成同时覆盖三代
        (aiduMEI|aiduMEM|duMem)，这条正则同时服务着在用端点。
    25. 新增 tests/test_v20_brand_policy.py 40 条，把这条分界线写成可执行的规则:
        五面 —— ① 10 个纯文档/样例文件整份归零（大小写敏感，故 AIDUMEM_URL 这类大写
        键名不会被误判成品牌残留）; ② 14 处运行时输出逐条点名「必须有新名、必须没有
        旧名」; ③ 以旧名开头的 logger 取用处（getLogger 调用）钉在 85 处; ④ AIDUMEM_* 键名钉成 72 键的冻结
        集，多一个就是新变量误用了旧前缀（新变量该用 AIDUMEI_），少一个就是既有键被
        改名、老客户的 .env 从此静默失配; ⑤ 6 个历史文件里旧名必须还在。第 ④ 面的洞
        是负向对照自己撞出来的: 集合相等只看名字在不在，只改读取处而文档照旧提这个键
        的话集合毫无变化、守卫照旧绿灯，运行时却已经读不到值了。于是补上第六条断言 ——
        AIDUMEM_API_TOKEN（鉴权）、AIDUMEM_UI_PASSWORD、AIDUMEM_STRICT_TENANT（v20 的
        记忆域隔离开关）、AIDUMEM_LEGACY_USER_IDS、AIDUMEM_DATA_DIR、
        AIDUMEM_ROUTER_DB_PATH、AIDUMEM_SQLITE_VEC_PATH 这 7 个「读不到就静默回落、
        且后果严重」的键，必须仍然出现在真实的 os.environ.get 里，光在文档里被提到
        不算。空表/文件搬家会让逐条断言变成空转，故另有一条断言表非空、键数为 72、
        所指文件都在。负向对照 7/7 双向成立: 露脸面回填旧名、启动横幅退回旧名、
        ducky/hot/health.py 的 logger 被顺手改、既有键全量改名、新键误用旧前缀、
        只改 ducky/facts_recall.py 的读取处而文档不动、CHANGELOG.md 被「清理干净」,
        七种各自命中该红的那一条，且每次改完按 sha256 逐字节复原。
    26. 仓内仍有 302 处 aiduMEM/duMem，是有意留下的，不是漏改:
        这个数刻意不含 CHANGELOG.md 与本文件这两个流水账 —— 它们逐版本引用旧名本就
        应该，且一旦算进来，「把总数写进流水账」这个动作本身就会改变总数，写下的
        数字当场作废。剩下 302 处分五类 —— 源码 docstring/注释/logger/线程名/键名/
        种子值 171 处（v19.4.2 决策 D2 早已定为不动，本轮只把 api_server.py 里那段
        策略注释更新成 v20 的说法并指向新守卫）、tests/ 78 处（守卫必须继续引用旧名，
        否则断言自己就死了；本轮新增的那个守卫文件就占 30 处）、docs/ 下有日期的设计
        文档 37 处、根目录两份白皮书 15 处、README.md 的「品牌演进」那一句 1 处
        （把 aiduMEM → aiduMEI 改写成 aiduMEI → aiduMEI 只会得到一句废话）。另记一笔:
        Hermes 插件那几处改动要等宿主自己重启才生效，本轮不动宿主网关。用例总数
        702 → 742（无宿主 730 passed + 12 skipped），README.md 与 README_EN.md
        的 16 处数字同步。
    27. scripts/release_scan.py 的用法串补上「单文件也行」—— 能力只写在源码里等于
        没写: 第 15 条给 scan_tree 补了单文件目标，连 docstring 和守卫
        (test_single_file_target_is_actually_scanned) 都有，却漏改两行给人看的提示 ——
        拒绝未知选项时说「只接受若干目录路径」、无参数时说「用法: <目录>」。于是这项
        能力对使用者不存在。代价当场就付了: 本轮扫品牌面时本可把变更文件直接喂进来，
        却因提示写着「目录」而先摆进临时目录再扫 —— 多一道搬运就多一份「扫的到底是不是
        那批文件」的疑问，而这恰恰是脱敏闸门唯一要回答的问题。两处提示改为「目录或文件
        均可」并在代码里留下因果；test_release_hygiene.py 22 条全绿，单文件正向对照实测
        报「已扫 1 个文件」、退出码 0。
    28. tests/test_v20_brand_policy.py 的源码清单从 git ls-files 改为按目录走盘 ——
        品牌守卫此前只在开发机上有效。生产是**拷文件部署**的: 那台机器的 .git 停在
        旧提交（实测索引 231 条 vs 磁盘 282 个文件），git ls-files 不报错、只少报，
        51 个 v20 新增文件一个都不在清单里，冻结集/logger 处数/高危键读取点三条
        守卫照常全绿，却根本没查新增的那一面；而在 sdist 解出来的目录里它直接
        128 退出（实测该文件 10 条用例全红）。跑不通看得见，少查看不见 —— 后者才是
        这个文件开篇警告的那种失败。改为 os.walk + 目录黑名单（含 data/backups/venv）
        与文件后缀黑名单（.db/.log/.pem 等），真 .env 跳过而 .env.example 保留。
        代价是白名单会过期，故补守卫 test_source_file_list_has_no_blind_spot_versus_git:
        有 git 的地方反验「git 认的、盘上还在的，走盘一个不少」，单向 —— 反方向在
        索引偏旧的生产机上必然不成立，拿它当红线只会在用户机器上误红。
        负向对照实测: 把 ducky/ 塞进跳过名单，守卫报出 112 个失明文件。
        用例总数 742 → 743（无宿主 731 passed + 12 skipped），README.md 与
        README_EN.md 的 18 处数字同步。
    29. benchmarks/download.py 新增必填 --source-commit，数据溯源从「锁哈希」补到
        「锁上游提交号」—— 只有哈希，第三方复核对不上时分不清「取错了版本」还是
        「文件被人改过」，而两个数据集的标注都在持续修。7–40 位十六进制，tag 名与
        分支名一律拒收（会移动的标识当不了先验承诺）；空值与 PENDING 也拒绝登记 ——
        最坏的是随手给个占位符，formal 闸门只查「非空且非 PENDING」，占位符正好骗得过。
        benchmarks/run.py 的 formal 闸门同步拒绝缺号开跑，且排在哈希校验之后 ——
        哈希不符是更根本的问题，先报它报错信息才不误导。benchmarks/PROTOCOL.md §1
        写明取法（上游检出目录 git rev-parse HEAD）。
    30. 新增 benchmarks/corrections.py —— 上游标注要修，就必须钉版本、钉数据哈希、
        配零修正基线，且改不动答案正文。此前只承诺「原始数据一个字节不改」，没给
        合法修正路径；没有合法路径，将来只会有不留痕的路径。三道硬约束: 必须有
        manifest_version（没版本号的清单能悄悄变，成绩无法复核）；非空清单必须钉
        applies_to_sha256（门槛正好落在「能改动数字」那一刻）；只许 add_evidence /
        mark_adversarial，碰 answer/question/adversarial_answer/text 一律拒收。
        另: 匹配不到目标必须报错（静默失效的修正让报告说谎），修正只作用于内存副本。
        benchmarks/run.py 补敏感性闸门: 带非空清单跑正式成绩必须同时给出同数据、
        零修正的基线 summary，否则拒绝启动。方向上诚实交代: mark_adversarial 只可能
        抬高我们的数字，正因单向有利才更要钉死。随仓库发零条清单
        benchmarks/corrections/locomo_v0.json。过程中抓到两个自己的问题: 其一
        mark_adversarial 一度是空承诺（标记写了没人读），遂接进召回判定
        并补正/负对照；其二我自己的校验报错顺序把红线盖住了 —— 正文字段本身也算
        「未知键」，先报未知键就让「想改答案」被一句「拼错了」顶掉，改为特定在前。
        tests/test_v20_benchmarks.py 新增 12 条。
    31. scripts/vector_shadow_poc.py 新增 --scale，向量后端从趋势判断补到实测曲线，
        docs/ADR-001-vector-backend-contract-and-poc.md 落表。此前只跑到 5000 点。
        实测（维度 64/每档 20 查/top-k 10）: sqlite-vec 路径 p50 12.18 → 125.71 →
        1332.41ms（每 10 倍规模约 10.3–10.6 倍耗时，干净 O(n) 全表扫描），同规模
        Qdrant 0.19 / 1.03 / 9.72ms，100k 时相差 137×；而三档 recall@10 全 1.000。
        正确性满分与性能不可用同时成立。三条量化口径进文档: recall 的标准答案是
        numpy 精确余弦而非另一个后端（后者只能证明「它们一致」）；每档一个子进程，
        因为 ru_maxrss 是进程累计峰值、只增不减，同进程混跑就是拿大档数字冒充小档，
        且该字段单位随平台变（macOS 字节、Linux KiB），不换算会把 Linux 报小 1024 倍；
        规模档是测量不是判定，少一档非零退出。tests/test_v20_vector_migration_poc.py
        新增 6 条，含文档护栏 test_adr_scale_table_matches_script（脚本改档而文档没改，
        读者会拿 100k 的结论去推一个从没跑过的规模）。
    32. ducky/hot/add.py 删掉一个多余的内层 import json —— 生产沙箱按 282 个文件逐一
        比哈希，只对上这一处不一致，采纳生产侧那份、两边归一。这条本身微不足道，
        值得记的是核验方式: 部署后逐文件比哈希，而不是「测试全绿就算部署对了」——
        测试绿只证明代码能跑，证明不了两边跑的是同一份代码。同轮清掉跨平台传输带进
        沙箱的 AppleDouble 伴生文件（._*），它们会让按目录走盘的守卫读到非 UTF-8
        字节而报 UnicodeDecodeError；此后 macOS→Linux 打包统一带 COPYFILE_DISABLE=1。
        用例总数 743 → 761（无宿主 749 passed + 12 skipped），README.md 与
        README_EN.md 的 18 处数字同步。
    33. 新增仓库根 conftest.py: 全套用例的数据目录隔离。起因是我们自己把测试行写进了
        生产库 —— ducky/utils.py 里 DATA_DIR 在 import 那一刻定型，于是「在一棵已部署
        的树里跑一遍 pytest」会让走真落盘的用例写进那棵树的生产库。v20.0 验收当天真的
        发生了: 生产 data/workspace.db 多出三条 alice 测试行、data/qdrant/ 多出一个
        无人持有的 .lock，而套件报的是 760 passed。这缺陷能活到今天，是因为写进去的
        样子和没写进去的样子在退出码上一模一样。修法: 在任何 ducky 模块被 import 之前
        把 AIDUMEM_DATA_DIR 与 AIDUMEM_LOG_DIR 一并改指到本次会话的临时目录，无条件
        生效（不「尊重」环境里原有的值 —— 生产配置本身就指着生产数据），只留一个响亮
        的逃生门 AIDUMEI_TEST_ALLOW_REAL_DATA_DIR=1。新增
        tests/test_v20_test_data_isolation.py 8 条盯着护栏本身，负向对照证明它能被
        关掉、且只能被那一个显式值关掉（"0"/"false"/""/" 1" 一律按没开处理）。
    34. 上一条护栏的第一版自己造了一次事故: 护栏的 bug 会伪装成产品的 bug。首版清理是
        无条件 atexit 删目录，而套件里有用例会再起一个 pytest 子进程（README 用例数
        护栏要 --collect-only 数一遍真实用例数）。子进程继承 env、沿用父进程的目录，
        退出时把父进程正在用的数据目录整棵删了。报出来的却是 ducky/wal_engine.py 的
        FileNotFoundError: .../wal/mem_mutations.wal ×5 —— 看着像「产品在新克隆上建
        不出 WAL 目录」这个毫不相干的缺陷，而 WALEngine.__init__ 明明 mkdir 过。前一轮
        据此记下的「第二处产品缺陷」是误判，此条撤回: ducky/wal_engine.py 与
        ducky/utils.py 一行未改，「克隆即跑」本来就成立。定位靠六次探针逐次否证，最后
        一次只记 True→False 那一次状态翻转（不是记「哪些时刻是 False」），一跑就点出
        了名字。两个措施焊进代码: _redirect 改为返回 (目录, 是否本进程新建)，atexit
        只在「自己建的」时候注册 —— 清理必须问「这目录是我建的吗」，而不是「我知道它
        在哪」；并补两条真子进程对照，一条钉「继承来的目录退出后必须还在」、一条钉
        「自己建的目录退出后必须没了」，"从不清理"和"清理越权"哪边都过不去。另记两个
        测量陷阱: -p 加载的插件早于 conftest 执行，那时读 env 是空的（探针 1、4 假
        阴性）；进程内 monkeypatch shutil.rmtree 看不见子进程的删除（探针 2 假阴性）。
        用例总数 761 → 769（无宿主 757 passed + 12 skipped），README.md 与
        README_EN.md 的 18 处数字同步。这一条是测试设施修复，不是产品修复。
    35. 同一个护栏第二次张冠李戴，且现场只在用户那台机器上: tests/test_hermes_plugin.py
        用 os.environ.pop("AIDUMEM_DATA_DIR", None) 收尾 —— 本意是擦干净，实际是「删掉」
        而不是「还原」，抹掉的正是根 conftest.py 设的隔离目录。红的却是四百条之后的
        test_root_conftest_exists_and_redirected_the_env（AIDUMEM_DATA_DIR 未被设置），
        报错指着无辜的人。更阴的是本地永远复现不出来: 该文件在没装宿主的开发机上整份
        skip（本地 12 skipped、生产 1 skipped，差的就是这 11 条），本地全绿、生产那台
        一跑就红 —— 只有用户会遇到的失败是最贵的那一种。这次定位没靠二分，一句
        grep "environ.pop" 直接落到行上: 问题形状（有人把它删了）本身是静态可搜的。
        三个措施: ① 肇事两处改用 unittest.mock.patch.dict（进出成对，连「原本没有」也
        能还原），顺手修掉同文件 test_config_beats_env 里同一写法；② 根 conftest.py 加
        一道 autouse 对账闸，每条用例跑完核对隔离环境变量，先还原（不让后面的用例连坐
        炸出一串假红）再断言（不让肇事者混过去）—— 谁改坏的谁红；③
        tests/test_v20_test_data_isolation.py 补两条: 一条真起 pytest 子进程做正负对照
        （不动环境的绿、抹掉的红，且必须点名肇事用例 —— 只验「抹掉会红」证明不了分辨力），
        一条静态兜底扫全套用例文件禁掉这种写法，并用「种一个违规文件必须被抓到、
        monkeypatch 写法不许被误伤」自证尺子量得准。静态那条不能省，因为运行期的闸门
        管不到「整份 skip 的文件」—— 这次撞的就是这个盲区。用例总数 769 → 771（无宿主
        759 passed + 12 skipped），README.md 与 README_EN.md 的 18 处数字同步。
        这一条同样是测试设施修复，不是产品修复: ducky/ 下一行未改。
    36. 拿本版自己写的基准回头逐条对账，查出两处漏项 —— 都是「门禁根本没法表达」级别的
        缺口。① 基准 §3.2 点名 VectorBackend 七个方法（upsert/search/delete/count/
        health/snapshot/restore），实现里只有六个: restore 从头到尾没写。一个只能备份、
        取不出来的抽象，等于让 G5「恢复演练」与 G7「备份可恢复」两项门禁在这一层压根无法
        表达 —— 不是没跑，是没得跑。补 SQLiteVecBackend.restore() 时把顺序焊死: 全部校验
        发生在覆盖之前（源不能是当前库自身 → 文件必须存在 → 只读探针 SELECT COUNT(*)
        FROM vectors 必须打得开），因为一次「先清空、再发现快照是垃圾」的恢复比不恢复更糟，
        那是拿一个坏备份把生产数据擦掉；覆盖之后再做一次后置条数对账，抄少了也算失败，
        不许「部分恢复」冒充成功；返回值是 int 而不是 None，让「什么都没恢复出来」没法被
        读成「恢复成功」。QdrantBackend 与它的 snapshot 对称地拒绝，不提供一个「看着像
        恢复」的假实现。② 基准 §8.1 要求 schema_version 可读，/health 里没有这一栏。补的
        时候躲开一个坑: 只回显代码里的 CURRENT_SCHEMA_VERSION 是假绿灯 —— 库还停在旧版本
        时它照样报新版本号。所以 schema_version 取磁盘上的 PRAGMA user_version（真相），
        代码期望值另开一栏 schema_version_expected，两者不一致时 schema_version_ok=False
        且记名降级。③ 自己捅出来的回归，而且失败形状是反的: 新探针里顺手写了一句函数内
        from ducky.degradation import DegradationTracker，Python 于是把这个名字变成整个
        函数的局部变量，模块级那处 import（ducky/hot/health.py:35）被遮蔽，于是版本对得上、
        走不到那个分支时，函数末尾原有的引用直接 UnboundLocalError —— /health 在「一切正常」
        的情况下 500，四条鉴权门禁用例连坐变红。删掉那句局部 import 即修复，把教训留成注释
        钉在原地。护栏的 bug 会伪装成产品的 bug，这次它还伪装成了「只在健康的时候才坏」。
        新增 9 条用例，两处各配负向对照: 把 restore 里的写回改成 no-op，往返演练必须红 ——
        顺带发现返回值断言（restored == 2）会碰巧对上，真正有鉴别力的是 payload/ID 逐项
        比对那一句；把代码期望值抬高一档，schema_version_ok 必须翻成 False（这同时证明了
        schema_version 不是常量回显）。用例总数 771 → 780（无宿主 768 passed +
        12 skipped），README.md 与 README_EN.md 的 9 处数字同步。
    37. 跑测越界写入用户家目录，被「跑测前后家目录快照对账」当场揪出:
        tests/test_v19_4_1_backup_gate.py 的 _persistent_root 取的是 pathlib.Path.home()，
        七条用例一直在 ~/.aidumem_test_backups 下建目录。理由正当（备份门禁铁律拒绝 /tmp 系
        备份根，而 pytest 的 tmp_path 恰在 /tmp 下），越界规模也小（带命名空间、每条用例
        finally 自清、查到时是空的），但父目录从来不删，且我们在文档里说的「沙箱跑测不碰宿主」
        对这七条从来没成立过。修法不是禁止碰 home（那会连正当理由一起禁掉），而是留改道口:
        AIDUMEI_TEST_BACKUP_HOME 覆盖根位置，不设时行为与从前逐字节一致。对照实验两组 ——
        设了改道口则七条逐条 PASSED 且真实家目录洁净，不设则家目录里重新出现；后一组才证明
        前一组的洁净是改道口的效果而不是巧合。
    38. 护栏的判据被自己的文档字符串喂假了: 为防回归在
        tests/test_v20_subprocess_env_isolation.py 新增规则三（凡取家目录的测试辅助函数都必须读
        一个 AIDUMEI_* 改道项），第一版判据写成 "AIDUMEM_" in ast.get_source_segment(...) ——
        而该函数返回的整段源码含 docstring，改道项的名字又恰好写在 docstring 里。负向对照当场
        露馅: 删掉真正的改道口代码、只留提到它的注释，护栏照样全绿；合成样本咬得动、真文件咬不动，
        因为真文件里有注释而样本里没有。数提及不是数位点。改成 AST 结构判定后（_refs_home 认
        Path.home()/expanduser/environ["HOME"] 的调用与下标节点，_reads_aidumem_env 认
        os.environ.get/os.getenv/environ[...] 的实参常量，注释在 AST 里不是这些节点），负向对照
        随即变红并点名 _persistent_root，并焊进一条 bad_comment_only 样本让该漏洞无法复发。
        新变量按品牌命名用当前前缀 AIDUMEI_（AIDUMEM_ 是为兼容既有部署冻结的旧前缀，
        不给新变量用），这一点是 tests/test_v20_brand_policy.py 的环境变量冻结守卫查出来的。
        用例总数 842 → 845（本机 833 passed + 12 skipped），README.md 与 README_EN.md
        各 17 处数字同步。

    39. 把一次 13 分钟的线上事故补记进公开变更记录 —— 本版第 32 条曾把它写成「这条本身
        微不足道」，那个判断是错的，原条目按「历史只新增不改」保持原样，在这里补记。
        事实是: /add 从 21:30:27 到 21:43:09 连续返回 195 次 HTTP 500，直到 21:44:23
        重启才恢复；期间 systemctl is-active 始终回 active，没有任何一处监控变红。
        根因是 ducky/hot/add.py 里一个多余的内层 import json —— 它把 json 变成整个函数
        体内的局部名，函数一进来就处于未绑定状态，走哪条分支都炸。第 32 条把注意力全
        放在「怎么发现两边代码不一致」上（那半截没错，逐文件比哈希确实比「测试全绿就算
        部署对了」硬），却把它已经造成的后果漏记了。补这一条不是为了改口径，是因为
        「我当时判断错了」本身比那行 import 更值得留档。
    40. 事故真正的教训不在那行 import，而在冒烟的形状 —— 出事当时部署后冒烟是**绿的**，
        因为它只打 /health /stats /facts/*，**没有一条打过 /add**。进程活着不等于接口
        活着；冒烟的覆盖面不含炸点，它给出的绿灯就是假绿灯。本版给
        tests/integration_smoke_api.py 补了写—读闭环，5 个端点扩到 7 个:
        test_06 真打 POST /add（带 infer=false 绕开 LLM 抽取那段 —— 事故是函数作用域的
        名字遮蔽，不抽取一样复现），test_07 真打 POST /search 读回。判据取
        「2xx 且 status != error」而不是 code == 200: 「200 但 status=error」是介于
        200 和 500 之间的第三种静默失败形状，只判状态码会漏掉它。断言故意留松（不钉
        响应体结构），因为 /add 的同步返回会随 mem0 版本变形，钉死了会为无关升级变红
        —— 红多了人就会把整条冒烟关掉，那才是真正的损失。
        写进去的是 __smoke__ 前缀 + pid + 时间戳的合成租户，跑完在 finally 里自删（断言
        失败也要删，否则反复跑会在库里越堆越多）；清理三层护栏: 前缀不符直接拒发删除
        请求、优先按 memory_id 精确删、任何 /delete_all 都**不传 confirm** —— 这里故意
        不给自己开二次确认的权限，万一租户名误解析成默认租户，服务端 ducky/hot/crud.py
        既有的守卫会回 400 而不是清库。清理失败只告警，绝不染红 PASS/FAIL。
        补完配了四条负向对照，用 stdlib HTTPServer 起桩逐一实跑: ok 模式 7/7 通过退出 0；
        500 模式（桩直接回事故当时那句 name 'json' is not defined）test_06 当场变红、
        退出 1 —— 旧冒烟面对同一台桩会退出 0；200+status=error 模式同样变红；第四条把
        前缀改坏，桩收到的请求数为 0，证明护栏真的拦得住。桩同时打印了收到的请求体，
        坐实了两件读代码只能猜的事: infer=False 确实发出去了，两条清理请求体里确实都
        没有 confirm。
    41. 给两处「故意不做域隔离」的代码写上它为什么不做 —— ducky/evolve_mem.py 的
        run_evolution_cycle 和 ducky/routes_evolve.py 的 POST /evolve/cycle 都用无
        user_id 过滤的全表扫描。本版全量做域隔离，这两处扎眼: 下一个人看到「一个没有
        域过滤的全表 SELECT」会以为是漏了，顺手「修好」。而给它加上域过滤才会真出事故
        —— 每个域各自衰减，冷热判断的样本被切碎，全局衰减基准就失准了。把「这是全库
        维护作业，不按域隔离」写进两处文档字符串并互相指向，是缺陷预防不是注释洁癖:
        一处有意为之的不隔离，不写下来就等着被当成 bug 修掉。
    42. bank_contract 的 include_legacy_aliases=True 从「静悄悄放宽」改成「必须留痕」——
        这个开关会把作用域判据从单看 user_id 放宽成 user_id OR source OR agent_id，
        是历史数据迁移期的兼容口子，走它就等于当次查询不设域边界。本版给它加了
        运行时 warnings.warn(RuntimeWarning) + logger.warning 双路留痕，并新增
        tests/test_v20_legacy_alias_guard.py（6 条）做静态守卫: 全仓扫描，任何未登记
        的调用点必须红。刻意没有改成 raise —— 真正的迁移脚本还得用它，堵死了人只会
        绕过去写第二份放宽判据，那就彻底失控了。静态分类器只认字面量 True 的关键字
        实参，宁可漏报也不误报: 误报会逼着后人往白名单里塞假条目，名单一脏就整个废掉。
        白名单当前为空，并配了「白名单不许烂成同义反复」的自检。
        用例总数 845 → 851（本机 839 passed + 12 skipped；生产机沙箱 850 passed +
        1 skipped，两个数都是实测），README.md 与 README_EN.md 各 17 处数字同步。
    43. 移除随附的 17 张界面截图（16M），控制台功能改由文字承载 —— 截图会随版本迅速
        过时，且容易把演示数据当成产品承诺。README.md 的控制台章节六段描述逐条扩写，
        把每一屏实际显示什么讲清楚；README_EN.md 此前**根本没有控制台章节**（只有三处
        顺带提及），本版补齐同构的英文章节。截图工具 tools/shot.js 保留 —— 它对被删目录
        没有路径依赖，能力还在，需要出图可自截。删除前做了两层零引用证明: 全仓搜
        docs/screenshots 为 0，再逐个拿 17 个裸文件名全仓搜（排除该目录本身）也为 0。
    44. 删截图时撞出一个假绿灯，顺手焊死 —— 删完做负向对照: 往 README.md 里植回一处
        指向已删图片的 <img>，跑全量 **843 全绿，没有任何守卫抓到**。这不是假想缺口:
        docs/README_draft.md 里 16 处 <img src="docs/screenshots/…"> 写在 docs/ 目录下的
        文件里，相对路径解析成 docs/docs/screenshots/…，**从 v19.4.0 写下那天起就是坏链**，
        全量测试一次都没红过。坏链的代价不对称: 仓库自测永远绿，坏的只有别人打开页面
        那一刻看到的碎图。新增 tests/test_v20_doc_asset_links.py（5 条）: 全仓 .md 逐份
        按自身目录解析本地引用并落盘 stat，<img src> 与 ![](…) 两种语法都认，外链不入
        射程（那要联网，不该混进单元测试）。配了 md 数与引用数双地板，防止有人把遍历
        收窄成「全绿但什么都没查」。用例总数 851 → 856（本机 844 passed + 12 skipped；
        生产机沙箱 855 passed + 1 skipped，两个数都是实测），两份 README 各 17 处数字同步。
    45. P3-8 最小权限: 发货的部署物不再默认以 root 运行 —— deploy/aidumem-api.service 与
        deploy/aidumem-sync.service 此前都写着 User=root，Dockerfile 连 USER 指令都没有。
        生产 systemctl show 实测确认不是纸面问题: NoNewPrivileges=no、ProtectSystem=no、
        CapabilityBoundingSet 是全集 41 项 —— 一个只在回环上读写记忆的服务握着
        CAP_SYS_ADMIN/CAP_SYS_PTRACE，只是把一次依赖链 RCE 从「丢记忆」放大成「丢整机」。
        API 单元改专用账号（User= 与 Group= 都显式写，只写 User= 时 systemd 取主组，
        而数据目录按组交接，主组不同名的症状是「读得到、写不进」），capability 全清、
        ProtectSystem=strict + ReadWritePaths 只开 data/+logs/、SystemCallFilter=
        @system-service、出站默认只放回环；docker-compose.yml 补 cap_drop ALL +
        no-new-privileges。**aidumem-sync 刻意不换 uid**: 它读的 MEMORY.md 通常是真人
        家目录里的 600 文件，换 uid 就得放宽那个文件的权限 —— 用「私有笔记可读面变宽」
        换「守护不是 root」不划算，所以只加固能力面，User= 留给部署方决定。同样刻意不启用
        ProcSubset=pid: ducky/resource_probe.py 读 /proc/self/status 取 RSS，那是 /health
        上唯一的内存指标，不拿唯一的可观测入口换一分暴露分。**降权不是把每个数字调到最小，
        是把每一项单独算清收益和代价。** 三个绊人的前提写进模板注释（装的人不会翻 CHANGELOG）:
        ① data/ 要整棵 chown —— WAL 要在同目录建 -wal/-shm，只交接主库文件时只读查询全绿、
        /health 也绿，直到第一次写入才炸；② logs/ 必须和 data/ 一起进 ReadWritePaths ——
        StandardOutput=append: 落在只读挂载上时单元直接 failed，只留一句 Read-only
        file system，极易误判成磁盘故障；③ cron 脚本要一起降权 —— ducky/utils.py 在
        import 时就 ensure_evolution_tables() 建连，所以哪怕 scripts/consolidator.py
        全文 sqlite3|.db 命中 0 行（纯 HTTP 客户端），import 了它就会以当前身份打开
        facts.db 并可能建出 root 属主的 -wal，留一个 root 写手就会周期性把整棵目录重新
        污染成混属主。容器 uid 写死 10001（bind mount 不做 uid 映射，uid 不固定则镜像重建
        后宿主机 data/ 突然写不进），只 chown data/+logs/ 不 chown 整个 /app —— 代码目录
        保持 root 属主只读，免费拿到「运行期改不了自己代码」，代价只是 __pycache__ 写不进
        （Python 静默降级）。新增 tests/test_v20_p38_least_privilege.py（首版 15 条，生产打回后 20 条）: 一律走
        unit 段落解析器而不是 grep（本轮注释里恰好反复出现 User=root、ProtectHome=yes，
        grep 分不清代码和注释）。三条是反向守卫，拦「照抄」和「刷暴露分」: sync 不许配
        ProtectHome（会藏起它要读的家目录，报 FileNotFoundError，像路径配错其实是沙箱）、
        两个单元都不许配 ProcSubset、SystemCallFilter 不许比 @system-service 更窄
        （resource_probe 的 lsof 退路会 EPERM，有兜底不崩但永久丢掉 open_fds 指标）。
        负向对照: 四个文件换回改动前版本 15 条全红，换回新版 15 条全绿。
        上线当天生产又打回三条（第一版模板通过了全部 15 条守卫才出的），各补一条守卫，
        共 20 条 —— 守卫射程小于缺陷分布，又一次:
        ① 带着绿灯失能: useradd --no-create-home 之后 $HOME 指向不存在的目录，而 mem0
        SDK 在 import 期要往 $HOME 写缓存。is-active=active、/health status=ok，但
        degraded=['vector_backend'] 向量检索静默零召回，journal 只有一行 Permission
        denied: '/home/aidumem' —— 不是崩溃，是带着绿灯失能，按 failed 告警的监控一辈子
        等不到。修法 StateDirectory=aidumem + Environment=HOME=/var/lib/aidumem（手写
        Environment=HOME 在 ProtectSystem=strict 下照样写不进，必须由 systemd 建并 chown）。
        ② 「这个进程不写库」是对业务逻辑的正确描述、对进程行为的错误描述，而沙箱管后者:
        mem0_sync 业务上只读 MEMORY.md 再 POST，第一版只给 ReadWritePaths=logs，结果启动即
        sqlite3.OperationalError: unable to open database file 并崩溃循环。traceback:
        from ducky.utils import 两个常量 → ducky/__init__.py → recall_funnel → scoring
        → ducky/salience/__init__.py 模块级 _ensure_db()（无 try/except）→ 开
        data/salience.db。import 两个常量会拽进整条召回栈。
        ③ root 在自己机器上写不进一个目录: root 无视权限位靠的就是 CAP_DAC_OVERRIDE，
        而 CapabilityBoundingSet= 把它一起清了。负向对照实测: systemd-run
        -p CapabilityBoundingSet= -- touch <data>/x → Permission denied，不带该参数 → 成功;
        而它仍读得到 600 root:root 的 MEMORY.md（那是它自己的文件，属主权限不需要 DAC
        override）。解法刻意不是把 CAP_DAC_OVERRIDE 加回来（等于用特权绕过权限），而是
        data/ chmod 2770（setgid）+ 两个单元 UMask=0007 + sync 加 SupplementaryGroups。
        setgid 与 UMask 缺一不可: 只有 setgid 新文件 640 组不可写，只有 UMask 新文件属组
        是创建者主组、组对不上。安装说明的 chmod 750 因此改成 2770 并补存量文件 chmod g+w。
        生产实机验收（前后都由 systemd 自己算）: 暴露分 9.6 UNSAFE → API 1.7 OK /
        sync 2.0 OK，capability 41 项 → 0 项; /health status=ok、degraded=[]、
        vector_backend_ok=true、mem0_singleton=true、feature_failures=0; POST /search 200
        （3 条命中，证明 embed+rerank 出站未被掐死）、POST /add 200; 交叉写双向通过;
        全量 1090 passed，唯一红灯是设计绊线 test_no_machine_here_satisfies_every_axis
        （该机九轴齐备），改动前就在红、非回归。资源: API RSS=249MB/11 线程/38 fd（身份
        aidumem）、sync RSS=23MB/1 线程/10 fd; 整机内存 48%、负载 0.48、磁盘 38%。
        cron 的 consolidator.py 同批降权为 runuser -u aidumem（最后一个 root 写手）。
        用例总数 1091 → 1111（本机 1099 passed + 12 skipped，实测）。★ 本条只证明模板
        写对了，生效值必须在机器上用 systemd-analyze security 验 —— 配置写了不等于配置
        生效，这笔学费 v19.4.2 的 StartLimit* 已经付过。
    46. 那条绊线按设计红了，于是被拆掉: 「全轴齐备」从推导值变成实测值 ——
        tests/test_v20_skip_axis_census.py 里有一条刻意会失效的断言
        (test_no_machine_here_satisfies_every_axis): 只要本机九条跳过轴全部齐备就
        pytest.fail，报错原文写着「这是好事: 现在可以真跑一次全量，把 README 里
        「推导值，从未实测」改成实测值，并删掉这条断言」。它的存在方式就是准备好被自己
        废掉。2026-08-24 在生产实机上它红了 —— 九轴同时齐备，全量 1111 passed·0 skipped。
        照它说的办: 绊线拆除，两份 README 的「全轴齐备」换成带日期的实测值。
        只拆了绊线那一半: 同一函数里「每条登记的轴都必须有探测器」那条留下（轴从四条长到
        九条时若不补探测器，len(present) 永远小于 len(_AXES)，齐备判定静默失真，这个风险
        和绊线在不在无关），函数改名 test_every_registered_skip_axis_has_a_probe。
        另一条守卫是反转而非删除: 原守卫要求 README 写「推导值，从未实测」（因为更早一版
        谎称「生产实跑核验」，被自己引用的那次实跑当场证伪）。现在数字真跑出来了，它的字面
        要求本身变成假话。守的东西一字没变（绝对措辞必须经得起自己引用的那次测量），改名
        test_all_axes_number_is_measured_and_attributed，判据三条: ①不许再宣称推导值从未
        实测; ②宣称实测必须带日期（「实测过」不带出处和推导值一样不可证伪）; ③被证伪的旧
        等式（装上宿主＝全绿）永不回归，宿主只是九条轴里的一条。
        ★ 反转时踩到自己设的坑: 第一版判据写成裸词匹配「从未实测 not in zh」，当场把引述
        旧措辞留证据的两句一起判红 —— 而原守卫早写明「负向对照只盯宣称，不能连引述旧假话
        一起禁掉」。改成只禁加粗的宣称原文才对。引述历史是资产不是负债，判据要分得清两者。
        负向对照: 退回旧宣称→红; 宣称实测但抹掉日期→红; 恢复→绿。
        用例总数仍是 1111（只拆断言不删函数）。
    47. README 结构手术: 它此前是一份伪装成 README 的更新日志 —— 新访客看到的第一个二级标题是
        「v19.3.0 架构大一统」，而当前版本 v20.0 在结构里根本没露脸，正文还散落着 v16/v18/v19
        各自的「亮点」章节。历史该由 CHANGELOG 管，README 只回答「现在是什么」。两份 README
        按「读者第一眼该看到什么」重排，删掉 6 段整段搬来的版本史（内容一字未丢，CHANGELOG 里
        本来就有更完整的）。新增四章，都是此前只在口头、从未写进发布物的:
        ①为什么 v20.0 是大版本（与 v19.5.0 逐项对照: 纪律版 vs 架构版、零运行时变更 vs 数据面
        契约、约 700 用例 vs 1111）; ②为什么不再用希腊神话命名（三条非审美理由: 代号爬进机器
        契约、改名等于动契约; 四段版本号读不出轻重; 神格是承诺堆多了还不上。并说清不是否认
        历史 —— 谱系表整张留着，能力一个没删）; ③与同类系统的定性对照（11 项逐项打勾，两个
        方向都照实说: 零依赖/离线/亚毫秒明确标为对方强项，我们的联网/延迟/API 成本明确列为
        代价）; ④跑分态度: 本版未跑分，因此不宣称任何分数。
        对照表里两行「口径」不是攻击，是拿对方自己页面上的两个数字互相对照: 头条「<1ms」对应
        它自己速度表的「搜索 45ms、向量搜索 15ms（1000 条规模）」; 头条「98.9% LongMemEval」
        是 Recall@All@5，而同页端到端问答是 65.2%。据此说明 1ms 是数据库裸读一条记录不是语义
        检索（不可能在 1ms 内跑完 cross-encoder 重排或 LLM 抽取，那个数字本身就是「没做这些」
        的收据），以及 Recall@k 与端到端准确率不是同一指标、接近 99% 通常意味着指标已饱和。
        ★ 删段时把一句必须存在的诚实边界一起删了，被守卫当场抓住:
        test_yellow_a_readme_claims_are_consistent 要求 README 必须明示「单机自托管」定位 ——
        防止读者把「多租户」过度理解成 SaaS 安全边界（过度理解会造成真实的安全误判）。中文版
        丢了英文版还在两处，现已补回并放在比原来更该在的位置: 紧跟在宣称「多租户」的下一行。
        守卫比我记得牢。
        ★ 顺手废止一条已过期的守卫: 同一测试还断言「从未实测 in readme」。那个数字已于
        2026-08-24 实测，继续要求 README 写「从未实测」等于要求它撒谎; 而它当时碰巧还是绿的
        —— 正文逐字引述旧措辞让裸词匹配照样命中。一条靠历史引述才通过的断言正是白护栏: 不再
        守着任何东西，只是还没红。已废止并原地留字条，职责移交 test_all_axes_number_is_
        measured_and_attributed。判据搬家要留字条，否则下一个人只看到「守卫少了一条」。
    48. 横幅改手写 SVG，图本身可复现: 原横幅 141KB 位图（另有 71KB webp 与 docs/ 下重复的
        141KB，共 353KB，其中两份已无人引用）。新横幅 assets/aidumei-v20-banner.svg 为 33KB
        矢量，配生成器 assets/aidumei-v20-banner.gen.py —— random.seed() 写死，重跑逐字节
        一致（已实测），所以这张图不是来历不明的二进制而是能 code review 的代码。背景是品牌
        标准六边形场，几何与配色照抄品牌站点不自创（#1f4e79/#525252/#000000，空心 fill:none，
        尺寸 15-85px、旋转 ±30°、描边 0.3-0.8、透明度 0.3）; 前景是一次跨越: 左侧一团未分化
        的小六边形 → 沿抛物线逐级变大成形 → 右侧收束为一个实心的域。图上没有一个字 ——
        横幅只负责让人一眼看到「跨了一大步」，文字交给正文。test_v20_gitignore_guard 的
        「必须发得出去」代表路径同步换成新横幅与生成器: 拿一个没人用的文件当代表，判据就空了。


v19.5.0 (脱敏闸门 · 把铁律变成不可绕过的程序 · 2026-08-20)
    核心主题: 一个坏掉的扫描器和一个干净的项目，报出来的东西一模一样 —— 都是「0」
    定性: **纪律版**。不改任何运行时行为，改的是「什么情况下才允许发布」。
    背景: 19.4.2 与 19.4.3 连续两轮都在同一件事上翻车 —— 脱敏做了，但做没做到位
    只靠人的记性和自觉。写在文档里的铁律，人会漏；漏了以后没有任何东西会红。
    更糟的是这类工具的失败是**沉默的**：词表少配一个词，扫描照跑、报告全绿、
    脏包上了公开索引，事后才发现。它不报错，只是安静地发放一张「已经检查过了」
    的凭证。
    方法: 把铁律从散文变成程序，并且给这个程序本身上锁 ——
    词表外置（绝不入仓，否则为防泄露而制造泄露）、空词表拒绝运行而非放行、
    负向对照焊进代码（自检不过就不准出结果）、豁免只认本行且必须出现在报告里。
    公开面同时从六面升为七面，补上最容易被忽略的「包索引渲染面」——
    它是元数据渲染出来的网页正文，**不下载就能看见**。

    1. scripts/release_scan.py 新增: 七面敏感内容扫描器。词表一律外置于仓库之外，
       模块内无任何内置词表兜底；词表为空或文件缺失一律拒绝运行（退出码 2），
       绝不输出那个与「真的干净」无法区分的 0。
    2. tests/test_release_hygiene.py 新增: 20 条守卫盯着闸门本身。核心是把扫描
       逻辑打坏成瞎子（什么都不报）、疯子（什么都报）、漏勺（豁免溢出全文件）
       三种故障，自检必须每一种都抓到 —— 一个永远通过的自检等于没有自检。
    3. 负向对照焊进代码: 每次真扫之前先在内置合成样本上做三向验证（脏样本必须
       报警、干净样本必须不报、豁免不得越行），任何一边不符即退出码 3，整轮作废。
       合成样本用假词，因此可安全入仓。
    4. 公开面六面升七面: 新增「⑥ 包索引渲染面」。同时明确发行包这一面没有退路 ——
       同一版本号在包索引上永久不可覆盖，扫描的位置只有上传之前一个是对的。
    5. tests/test_v19_4_governance.py: 验证凭据拦截规则的合成夹具加行内豁免标记。
       出口刻意做得很窄 —— 只认命中所在的那一行，无文件级、更无目录级豁免，
       且豁免仍逐条出现在报告里；从失败计数里消失，不从视野里消失。
    6. scripts/backup_gate.sh 与版本对齐: 备份根路径的默认值不再写死内部部署路径，
       改为相对仓库根目录；pyproject.toml、manifest.json、README.md、README_EN.md
       与版本真相源同步至 19.5.0，谱系补记本次发布。
    7. 闸门自身被实战打脸并修复: 首版 main() 用「不以 - 开头就是目录」挑扫描目标，
       结果 `--name X` 的选项名被丢掉、值 X 被当成目录 —— 而 X 并不存在，
       扫出 0 个文件，报告照样打印「无硬敏感命中」。一个手滑的参数换来一行绿色，
       正是本版要消灭的那类静默失败，首轮真扫时当场踩到。现改为：未知选项拒绝运行、
       扫描目标不存在拒绝运行（均退出码 2），并各补一条守卫钉住，含正向对照证明
       拒绝来自参数本身而非目录。

v19.4.3 (发布卫生 · 发行包也是公开面 · 2026-08-20)
    核心主题: 已发布的包永远改不回来 —— 所以扫描必须发生在上传之前
    定性: **v19.4.2 的等价版**，零行为变更。可执行逻辑与 v19.4.2 完全一致，
    差异仅在注释、docstring 与版本号本身。
    背景: v19.4.2 的源码注释与 docstring 里残留了内部部署环境的描述性文字。
    源码仓库这一面可以重写，**但发布到包索引上的同一个版本号永久不可覆盖、
    不可修改** —— 唯一的出路是撤回旧版、另发一个干净的版本号。
    这是所有公开面里唯一没有退路的一面，本版即为此而生。
    方法: 发布链新增强制卡点 —— 发行包必须解包实扫，且扫描器必须先在已知
    命中的对象上验证有效（负向对照）之后，那个「0 命中」才作数，才允许上传。

    1. ducky/version.py 与 pyproject.toml、manifest.json: 版本号提升至 19.4.3，
       谱系补记本次发布。
    2. README.md 与 README_EN.md: 版本标识与谱系表同步至 19.4.3。
    3. 源码注释与 docstring 清理: 移除对内部部署环境的描述性文字，
       不触及任何可执行逻辑（改动全部落在注释行与文档字符串内）。

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
        ⚠️ 本条首版把两个键写进了 [Service] 段 —— systemd 直接忽略，行为与没修一致。
        修正见下方 🔵-21。
    13. deploy/logrotate/aidumem: 用 copytruncate —— 单元是 StandardOutput=append:，
        改名切割后进程仍写旧 inode，日志凭空消失。
    14. pyproject.toml / requirements.txt: 补同步守护进程依赖声明（此前靠部署机恰好装过）。

    —— 🟢 品牌与版本 ——
    15. 前端品牌残留清理（标题 / description / alt / 字标 / 错误文案 / 图谱中心节点 / 注释）。
        字标是标签拆分写法 aidu<b>MEI</b>，全局 sed 扫不到 —— v19.4.1 的改名正是从这里漏出去的。
    16. /docs 的 FastAPI 标题改为 aiduMEI API。logger 名、/health 的 service 字段、
        各模块 docstring 里的 aiduMEM 一律不动 —— 机器契约与历史内部名，生产监控按其匹配。
        环境变量前缀 AIDUMEM_* 同理保持不变。
    17. 版本号五文件对齐 19.4.2，代号仍为 Athena · 雅典娜。

    —— 🔵 审计整改轮（用户视角审计 + 自查追加，同日）——
    18. scripts/dev_server.py 的**双重逃逸**：它既按目录逃逸（守卫的 _SKIP_DIRS 里
        写着 frontend），又按信号逃逸（用的是第 4 个上游变量名 AIDUMEM_UPSTREAM
        与第 2 个端口 8777，扫描器的特征串一个都不匹配）。两层都得拆掉才看得见。
        —— **目录级豁免是最容易积累盲区的写法**：豁免当初的理由（「这里没有可执行的
        调用方」）会随着目录里长出东西而悄悄过期，而豁免本身不会跟着过期。
        现改为按文件名精确豁免，并补齐凭据注入与 401/403 诊断分支。
    19. dev_server 启动 banner 从 stdout print 改为 stderr 单次写入 + flush。
        nohup / 管道下 stdout 是块缓冲的，banner 会一直躺在缓冲区里等到进程退出才刷出来
        —— 而「auth 到底加载没加载」恰恰是要在**启动那一刻**看的。改走 stderr 后
        与请求日志（log_message）同序，也不再需要 -u。
    20. dev_server 四个 do_* 方法收敛为一个 _handle_api() 骨架（重构，行为不变）。
        原先前缀判断与读 body 各写四遍 —— 凭据这类「必须每条路径都生效」的东西，
        最怕的就是这种复制粘贴：改一处要记得改四处。
    21. ★ **systemd StartLimit* 放错段**（本轮最严重，审计未发现，自查揪出）：
        这两个键只在 [Unit] 段被解析，写进 [Service] 会被 systemd 静默忽略
        （255 实测：Unknown key name ... in section 'Service', ignoring），生效值仍是
        默认 10s/5。配合 RestartSec=10，限流窗口内永远凑不满次数 —— 也就是说
        上面 🟠-12 那条「已修复」的配置，行为与完全没修一模一样。
        配置文件里白纸黑字写着、grep 查得到、review 看得过，却不生效：
        **配置写了不等于配置生效**。唯一的验收方式是问 systemd 自己算出来的值
        （systemctl show -p StartLimitIntervalUSec），而不是 grep 单元文件。
    22. deploy/aidumem-api.service 同补 [Unit] 段 StartLimit*（此前完全没有）。
        代价是连续崩溃后需人工介入 —— 这是刻意的：5 分钟崩 5 次的服务，
        自动重启只会把故障拖成静默的长期不可用。
    23. 新增 tests 守卫 test_no_unit_template_puts_startlimit_in_service_section：
        **按段**扫描 deploy/*.service，任何 StartLimit* 落在 [Service] 立刻红灯，
        并带正面锚点（[Unit] 段必须确有这两个键），防止守卫退化成永真。
        原有的 test_sync_unit_template_makes_crashloop_visible 一并加固 ——
        它此前只断言「字符串在文件里」，所以对 21 那个缺陷照样给绿灯。
    24. README 测试数字守卫扩面：原守卫只盯中文 README 的表格一行，于是首版改了表格
        却漏掉同页正文，README_EN.md 整段没动（数字互相打架，其中一个甚至推导不出来）
        —— 又一例「守卫的射程小于缺陷的分布」。现按 12 处逐一校验（中英 × 三行表格 +
        正文提要 + 两个复现命令块），任一处漏改立刻红。
    25. 「12 跳过」不再是手抄常数：它必须等于 tests/test_hermes_plugin.py 实际收集到的
        条数，宿主插件测试增减时 README 会跟着红 —— **自洽不等于属实**。
        两份 README 同时补上 HERMES_SRC=... 的复现命令：
        **跳过必须能被复现成通过，否则它只是一个没人能证伪的数字**。
    26. tests/ 下三个运维脚本（integration_smoke_api.py / integration_e2e_lifecycle.py /
        perf_baseline.py，住在 tests/ 但不是 pytest 用例）补 api_auth_headers() 与
        sys.path，并把各自重复的请求逻辑收敛为单个 _request()。
    27. 新增守卫 test_changelog_and_version_py_do_not_drift：CHANGELOG.md 与本文件
        记的是同一件事却各自手工维护，必然漂移 —— 本版首版就漂了（17 条 vs 16 条，
        差的那条谁也没发现，因为没有任何东西在看着这两份文件的关系）。
        现锁条目数相等 + 编号连续 + 本文件点名的路径 CHANGELOG 必须也有（单向，
        允许本文件把一组文件概括成一句话，不允许它提到详细版没写的东西）。
    28. 两个单元的失败策略改为刻意不同：API 3600/30，sync 300/5。第一版给两者写了
        同一套 300/5，是把两个目标相反的东西按同一个模子守 —— API 一停等于调用方
        当场失忆（第一价值是「在线」），sync 一停只是 MEMORY.md 晚点同步
        （第一价值是「被发现」）。放宽 API 又不牺牲可见性，靠的是崩溃循环**密集**
        （RestartSec=5，30 次仅需 150 秒，远小于 3600 秒窗口，照样进 failed）而
        偶发抖动**稀疏**（一小时零星十几次凑不满 30）—— 长窗口 + 大计数，对稀疏
        宽容、对密集仍敏感，不是二选一。沙箱探针实测：键写 [Unit] 的 25 秒后
        NRestarts=5 进 failed，键写 [Service] 的 70 秒重启 13 次仍是 activating。
        ⚠️ 第一版探针用 RestartSec=1，两边都 failed —— 1 秒一次连默认 10s/5 都能
        凑满，把「写错段」整个掩盖了。负向对照本身也会失效，它必须复现真实参数。
    29. ★ 「12 跳过」的复现命令此前只**单向**成立：25 那条说「跳过必须能被复现成通过」，
        做的却只有一半。宿主自动发现会命中 /hermes/hermes-agent，生产机上就摆着一棵，
        于是 README 第一条命令在这类机器上跑出来是 403 passed / 0 skipped ——
        读者根本没法把宣称的「12 跳过」复现出来。**双向可复现才叫可证伪**。
        根因是 tests/test_hermes_plugin.py 把环境变量和硬编码路径塞进同一个候选列表
        顺序匹配：既没有「强制关掉」这一档，又让 HERMES_SRC=/typo **静默**落到
        /hermes/hermes-agent —— 指了 A 却在测 B，还是绿的。**隐式回退会悄悄推翻
        显式意图**，与 18 的「目录级豁免」同类：随环境改变测试集合却不发一言。
        现改三态、显式永远压过隐式：未设→自动发现；none/no/off/0/false/空→强制无宿主，
        一条回退路径都不试；显式路径无效→报错并点名坏路径。两份 README 同补
        HERMES_SRC=none 一档与对称守卫，测试数 399→403、通过数 387→391。
    30. 新增 tests/test_v19_4_2_hermes_host_resolution.py（4 条，**刻意不带 skipIf**）：
        它守的恰是「纯净机 / 装了宿主的机器行为是否都可控」，若也随宿主缺席而跳过，
        在纯净开发机上就永远空转 —— **守卫跟着被守对象一起消失**，是本版反复踩到的
        同一个坑。全程用临时目录伪造宿主，一棵真源码树都不需要；「显式禁用压过自动发现」
        那条带正面锚点，否则「返回 None」可能只是因为压根没有宿主，断言会空转成永真。
        ⚠️ 刻意不放进 tests/test_hermes_plugin.py：那份文件的前提是「整份都随宿主缺席
        而跳过」，README 的「12」正由它的收集数推导 —— 掺进永不跳过的用例，收集数变 16、
        实际仍跳 12，守卫只会报「README 数字不对」，不会告诉你是它自己的前提被掀了。
        **别把守卫的地基当普通空地用。** 生产实测三态：未设→403 passed；=none→
        391 passed, 12 skipped（装着宿主的机器上，12 跳过第一次真正可复现）；
        =/typo→RuntimeError。

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

SERVICE_VERSION = "20.3.2"
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
    ("20.3.2", "", "v20.3.2", "正式版 · 五方外审整改 · 一致性与底层 · 2026-09-03（pre 09-01 · beta 09-02）"),
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

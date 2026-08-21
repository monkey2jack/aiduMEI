"""
ducky.version — aiduMEI 版本信息唯一真相源
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
所有版本号从这里导入，禁止在其他模块硬编码。

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
    18. frontend/dev_server.py 的**双重逃逸**：它既按目录逃逸（守卫的 _SKIP_DIRS 里
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

SERVICE_VERSION = "20.0"
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

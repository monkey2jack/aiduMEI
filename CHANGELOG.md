# aiduMEI 版本演进史

## [载体接入] Codex CLI 原生 hook 三线接通（2026-10-17）

> **性质：新增 Codex CLI（≥ 0.163）载体接入与文档，不改 aiduMEI 自身版本号、不打 tag、不发 release。** 不动服务端契约 / Qdrant schema / 决策模型 / 记忆数据，改动全部落在 `integrations/codex/`、aiduCAT 宿主配置与文档。Codex CLI 0.163 起原生支持 lifecycle hooks，事件模型与 Claude Code 对齐（`UserPromptSubmit` 读线 / `Stop` 写线 / `SessionEnd` 萃取线），因此三线架构原样复用——无需轮询会话库、无需解析 transcript。

- **三线真源入仓** `integrations/codex/` 三件：`aidumem-codex-inject.sh`（读线，`/api/core-memory` + `/search`）/ `aidumem-codex-ingest.sh`（写线，`/add`）/ `aidumem-codex-distill.sh`（萃取线薄适配层，复用久经生产验证的真源 `integrations/aidumem-distill.sh`，不重写萃取逻辑）。身份解析优先级与 Hermes / cc 版逐行同源，新增环境变量一律走 `AIDUMEI_CODEX_*` 前缀（`AIDUMEI_CODEX_SESSION_PREFIX` / `AIDUMEI_CODEX_SPOOL_DIR`），不碰 `AIDUMEM_*` 冻结集。
- **写线原话走 spool 配对（不走 transcript）**：Codex 官方明确声明 `transcript_path`「不是稳定接口、格式可能随版本变」，依赖它会重蹈「字段一变形就静默返回空」的覆辙。改为读线暂存 prompt、写线按 `turn_id` 取回（`AIDUMEI_CODEX_SPOOL_DIR`），transcript 仅作 spool 缺失时的尽力兜底，格式完全自控。
- **SessionEnd 萃取线后台化**：Codex 的 SessionEnd 超时上限 3s 且**始终同步**（`async:true` 无效），萃取真源内部 setsid 后台化、本层 <100ms 返回，提炼在后台完成，不阻塞宿主退出。
- **hook 信任门槛（最大静默失败源）**：Codex 非 managed hook 必须在 TUI `/hooks` 审阅并 trust 才运行，未信任时**静默不执行**——探针全绿但记忆不进。无头场景（aiduCAT bridge / CI / cron）没有 TUI，须由宿主在 `codex exec` 传 `--dangerously-bypass-hook-trust`；实测该开关写进 `config.toml` **不生效**，只有 CLI flag 有效（0.163.0）。
- **aiduCAT 宿主侧适配**：`profile-schema.ts` 新增 `codex.bypassHookTrust`，`argv.ts` 据此自动追加 `--dangerously-bypass-hook-trust`，`adapter.ts` / `agent-runtime.ts` 贯通，`codex-argv.test.ts` 补 2 条回归；tsc 干净、单测通过、`pnpm build` 完成。profile 里设 `codex.bypassHookTrust=true` 即可，无需手改 argv。
- **本机接线与端到端验收**：`/data/home/.codex/hooks.json` 挂载三线，并修复此前损坏的 `[mcp_servers.aidumei]`（改用 venv python + `mcp_server.py` + env token/user）。实测记忆沉淀带 `metadata._origin_session_id=codex-*`，萃取线 distill 日志 `source` 计数命中（session-id 逐字一致），仅因 turn 数未达阈值而跳过——符合预期，非失败。
- **文档**：新增 `integrations/codex/README.md`（安装 / 与 cc 版差异 / 验收 / 回滚），`integrations/INTEGRATION_GUIDE.md` 补「接入 Codex CLI」段与 Codex 回滚块，`README.md` 接入面表与三线对照表补 Codex CLI 行。

## v0.4.0.post2（对外 f0.4++ · 2026-10-16）

用例总数 4215 → 4229（2026-10-16 `pytest --collect-only`；新增 12 条 cc 适配回归在 tests/test_f04pp_cc_adaptation.py，另 2 条 cc 身份兜底回归在 tests/test_v19_4_2_identity_env_fallback.py）；这是收集数，通过结果以精确提交的运行回执为准，记录在 Release 说明。

本版是 cc（Claude Code）载体适配收口版：宿主从 Hermes 迁到 Claude Code 后，第一用户实机自检暴露的记忆回路缺口全部根因级修复，不更换模型与决策配置、不动 mem0 基座与向量库 schema、不做记忆数据迁移。新增回归均退回旧代码做过负向对照。2026-10-17 经维护者授权推大仓、打公开 Tag/Release，f0.4++ 由内部候选翻转为公开正式版（PUBLIC_RELEASE_VERSION 同步升至 f0.4++）。

- cc 萃取线接通（根因修复）：写线 integrations/claude-code/aidumem-cc-ingest.sh 沉淀 /add 补 `_origin_session_id`（`cc-` 前缀口径），SessionEnd 适配层 integrations/claude-code/aidumem-cc-distill.sh 从误调 `/session/start`（导致同一会话反复「Session 创建」、提炼从未发生）改为复用久经生产验证的真源 integrations/aidumem-distill.sh（两步走：/session/distill 只提炼 → /add 落库，继承幂等键、后台化与日志轮转）。此前 cc 写线不传 session_id，verbatim_turns 里 1387 条历史原文 session_id 全空，distill_session 永远圈不到来源——cc 会话精华自上线起从未产生（静默失败铁律的活案例）。实测端到端：3 轮原文 → SessionEnd → 提炼 queued job → 精华可召回（metadata `session_id=distill:cc-*`），且写线/萃取线口径由同一条断言钉死逐字一致。
- Stop 写线注入加固：原实现用 `r'''$INPUT'''` 把 hook payload 拼进 python heredoc，transcript 含三引号即破语法（写线静默失败）。改为环境变量传递；毒样本（三引号/双引号/反斜杠/`${}`/`$()`/大括号）逐字节完整到达 /add 有回归钉死（tests/test_f04pp_cc_adaptation.py）。
- journal 遗留收养工具 scripts/adopt_mutation_journal.py：宿主间整体迁移 data/ 后 mutation_journal.sqlite3 属「遗留库」，服务启动 fail-closed 报 `journal_legacy_adoption_required`（本侧 2026-10-16 09:39 连续 3 次启动失败实证，当时以一次性手工收养救火——不可复现、无回执、绕过了写入者停止核验）。工具将收养工程化：活体 API 守卫 + `.api-process.lock` 互斥 + 只读深度核验（quick_check/schema/状态集合/防重复收养）+ 幂等跳过 + JSON 回执；历史记录逐字节保留，收养后深度完整性核验 ok。负向对照：未收养遗留库仍拒绝启动、活体服务拒绝收养。
- 守护脚本 scripts/aidumei_watchdog.sh：qdrant 容器与 API /health 探活（crontab 每分钟一跳），容器不在即 `docker start`、进程不在即拉起（cd 铁律经 start-aidumei.sh），探活超时给足 5s 防预热期误判；拉起后 30s 复核、进程在但连续 5 跳未愈升级显著告警，绝不盲杀；每次处置写 logs/watchdog.log 留痕，正常心跳零输出防日志膨胀。
- 注入读线误熔断修复：integrations/claude-code/aidumem-cc-inject.sh 探活超时 0.3s → 1s（2026-10-16 19:10 服务高峰期写向量时 0.3s 探活误超时触发熔断实证；1s 仍远小于宿主 hook 8s 上限），相关性检索 query 改环境变量传递（原 shell 字符串拼进 `-d '{"query":"$PROMPT"}'`，prompt 含双引号即破 JSON）。
- 本地嵌入备胎就位：安装 fastembed（local-embed extra）并部署 BAAI/bge-small-zh-v1.5 模型（512 维 · 91MB · 离线加载与 512 维嵌入实测通过），dual_index_local 欠账重放循环报错消除；scripts/local_embed_model_sha256.json 的 files_metadata.json 条目按清单自带 unverified 机制降级——该文件是 huggingface_hub 自动生成的缓存元数据，随 hub 版本漂移而非模型内容变化（新环境取模期望 6a13b7… 实得 b48cf7…），模型本体 5 blobs + refs/main + trees 共 7 项逐字节 sha256 背书、revision 号一致，钉死元数据哈希会把换了 hub 版本的新部署全部误卡。
- 一键体检脚本 scripts/selfcheck_cc.py：把本轮全面自检固化（API 健康/版本对齐/Qdrant 各 collection 点数与 restart 策略/journal 证据链/hooks 三线挂载与语义/守护 crontab/fastembed/磁盘与数据规模），`--json` 机读、红项非 0 退出；改造前基线红 3 黄 2 全部如实报出，修复后逐项转绿。
- cc hook 真源入仓 integrations/claude-code/ 三件（aidumem-cc-inject.sh 读线 / aidumem-cc-ingest.sh 写线 / aidumem-cc-distill.sh 萃取线适配层）：宿主 ~/.claude/hooks/ 是拷贝部署（钩子是拷贝不是软链的历史教训），入仓后「仓库即真源」，升级重部署可用 scripts/check_hook_deployment.py 同款思路核对。
- 版本三方对齐：SERVICE_VERSION 0.4.0.post2（pyproject.toml / manifest.json 同步），FULL_VERSION f0.4++ 入 LINEAGE 谱系，PUBLIC_RELEASE_VERSION 升至 f0.4++（公开正式版）；部署时 .env 的 AIDUMEI_BUILD_SHA 写入本轮精确提交——此前运行的 dff08f3（2026-10-05）落后仓库 HEAD 三个 Release，/health 溯源字段与真实代码脱节。

## v0.4.0.post1（对外 f0.4+ · 2026-10-14）

用例总数 4156 → 4215（2026-10-14 `pytest --collect-only`；行为 3585、脚本/钩子 484、守卫 146）；这是收集数，通过结果以精确提交的运行回执为准，记录在 Release 说明。

本版只修 f0.4 上线后真实使用暴露的问题，不更换模型与决策配置。新增回归均退回旧代码做过负向对照。

- 供应商拒答不再锁死记忆域：2026-10-14 生产上一轮对话在抽取阶段被服务商拒绝（HTTP 451），日志记为 `repair_required`，随后同域新写入全部 409，同样的事 10-10 也发生过。mem0 的抽取错误发生在任何写入之前（`tests/test_f04p_prewrite_extraction.py` 用已安装 mem0 源码的调用顺序守卫钉住，并以故意前移写入的副本做负向对照），因此只对 `mem0_add` 上 mem0 自己的抽取 `LLMError` 记 `not_applied`（`provider_rejected_before_write`），作用域保持可写；确定性 `infer=False` 写入恢复工作（f0.4 的日志曾把这次尝试当作 SDK 已动手，导致降级被拒）。真实 mem0 + OpenAI SDK + 路由的端到端回归中，旧代码复现了 409。
- 内容拒答（451 或服务商内容策略码）不计入 LLM 熔断，回执为 `skipped_llm_content_rejected` 并附人话说明；503 等服务故障照旧计入，作为区分对照。
- 精华线适配 Hermes v0.22：宿主每轮触发 `on_session_end`，钩子改为后台作业并立即返回 `{}`，同会话 flock 互斥（无 flock 时由服务端幂等键兜底），诊断写入带时间戳的持久日志。服务端按新增来源节流（`AIDUMEI_DISTILL_MIN_NEW_SOURCES` 默认 6，`AIDUMEI_DISTILL_MIN_INTERVAL_SEC` 默认 7200），只提炼尚未覆盖的新段；`force` 用于 `on_session_finalize` / `on_session_reset`（后者以 `extra.old_session_id` 定位被结束的会话）。生产此前 79 个会话产生 280 条精华，同一程最多被重提炼 9 次。
- 新增 `POST /session/distill/prune`：默认只列出「来源被同会话更新一代完全覆盖」的旧精华；`apply=true` 走与 `/delete` 相同的级联和 WAL，枚举不完整时拒绝执行；按 delete 权限授权，路由普查已登记。
- 定时与子代理会话（钩子 payload 的 `platform`、插件 `initialize` 的 `agent_context` 为 `cron`/`subagent`）默认不写记忆、不提炼，读取不受影响；`AIDUMEI_SKIP_PLATFORMS` 可改，置空即恢复写入。插件对宿主的 `flush` 上下文始终不写，与 Hermes MemoryProvider 契约一致。
- 监控：`scripts/e2e_smoke.py` 的 `/add`、flush 与清理补齐 `caller_user_id`（f0.4 起该冒烟每小时 403）；`scripts/health_check.py` 输出带时间戳，可选 `AIDUMEI_ALERT_COMMAND` 在正常与异常之间切换时执行一次（同状态 6 小时抑制，非法配置回退默认），告警命令失败不改变巡检结论；写线失败另记 `aidumem-ingest.log`。
- `scripts/check_hook_deployment.py` 识别 finalize/reset 事件，并修正解析：hooks 下同级的其他事件不再被算作上一个事件的命令（旧解析会把无关脚本记到萃取线名下）。
- 文档：接入指南与 `config.yaml.snippet` 写明 v0.22 钩子语义（逐轮 end、finalize/reset 边界、30 秒回调上限、钩子是拷贝）、注册三事件与 25 秒超时；`.env.example` 与环境变量注册表补齐新变量。包版本采用 PEP 440 的 `0.4.0.post1`（品牌版本的 `+` 不进包版本，避免 Release 附件名把 `+` 改成 `.`）。

升级与回退见 [f0.4 运维说明](docs/F04_UPGRADE.md) 的 f0.4+ 一节。

---

> 📚 **更早版本（aiduMEM 白皮书时代 ~ v0.3.0，共 85 个章节）**：已整体迁至 [docs/CHANGELOG-archive-v9-v03.md](docs/CHANGELOG-archive-v9-v03.md)，内容零删改——旧品牌名 aiduMEM / duMem 的全部历史记载都在归档里，只增不改的传统由该文件延续。

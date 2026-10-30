<p align="center">
  <img src="assets/aidumei-banner.webp" alt="aiduMEI ☤ 爱嘟优忆思 — Memory + Engine + Insight" width="100%">
</p>

<!-- distribution-policy: github-source-only -->

# aiduMEI ☤ 爱嘟优忆思——给 AI 智能体用的长期记忆引擎

aiduMEI 是一个**单机自托管**的长期记忆服务（Python / FastAPI，MIT 许可）。它在你自己的机器上运行，通过钩子接到 Claude Code、Codex CLI、Hermes 等 AI 宿主：每轮对话前把相关记忆注入给模型，每轮对话后把这一轮存下来，会话结束时提炼一条「这一程」的精华。宿主管短期对话，aiduMEI 管长期记忆；数据全部落在本机数据目录，可备份、可删除、可恢复。

> **当前公开版本 f0.7。** 包版本 `0.7.0`，2026-10-29。[下载 Release](https://github.com/monkey2jack/aiduMEI/releases/tag/f0.7)。逐版本历史见 [CHANGELOG](CHANGELOG.md)。
>
> 当前代码 **f0.7.1**（f0.7 的审计修复补丁，内部候选，尚未公开发布独立 Release；公开 Release 仍为 f0.7）。包版本 `0.7.1`，2026-10-30，修复内容见 [CHANGELOG](CHANGELOG.md)。

## 本版变化（f0.7）

f0.7 是一次底座更换：记忆内核从 mem0 换成自研的 **native 单库 SQLite 内核**，mem0 不再是依赖。

- **native 内核**：一条记忆的正文、全文索引、向量、修改历史、墓碑、关系边写在同一个 `memory.db`，一次写入一个事务。**所有挡位默认用它**；唯一例外是从旧版升级、还没迁移的 mem0 部署，会自动留在 mem0 并在日志提示迁移（见[日常运维](#日常运维)）。
- **mem0 变可选**：`pip install -r requirements.txt` 不再安装 `mem0ai` / `qdrant-client`；只有要用旧 mem0 内核、从 mem0 迁移或回滚时才 `pip install -r requirements-mem0.txt`（或 `pip install ".[mem0]"`）。默认 native 进程里不加载 mem0。
- **一条命令接宿主**：`scripts/connect_host.py` 自动识别 Claude Code / Codex / Hermes，接好读、写、萃取三条线并当场验收。
- **心图体检与记忆星云**：控制台和 `GET /graph/checkup` 列出重复、过时、冲突的记忆；`GET /graph/nebula` 按需画出真实关系图。
- **设置页「测试连接」**：对 LLM / Vision / Embedding / Reranker / 决策模型逐个做一次最小真实调用；新增「用本地 vLLM」预设。
- **删除更彻底**：删一条记忆时，它对应的原话行、由它派生的事实、以被删原话为来源的会话精华一并清理；带相对日期的句子删后不再残留日期事实；仍可从墓碑恢复。
- **接口收敛**：HTTP 路由按白名单收窄，MCP 工具 41 → 6；不再宣称任何公开跑分名次，质量以真实使用回归集为准。

## 核心优势

每一条都能在仓库里找到对应代码或测试。

| 能力 | 说明 | 代码位置 |
|---|---|---|
| **native 单库内核** | 正文、FTS5 trigram 全文索引、向量、历史、墓碑、关系边同在 `memory.db`，同一事务写入，不需要单独的向量数据库服务，`/add` 不逐轮调 LLM 改写。检索 = FTS5 trigram（bm25 排序）与向量余弦两路，RRF 融合。向量走 OpenAI 兼容 embedding 接口；没配或在本地档时只走全文检索，正文照样能召回 | `ducky/memory_kernel/impl_native.py` |
| **原话保存** | native 内核把每条消息按原文入库，不经 LLM 改写；会话精华是另存的一条摘要，不替换原话 | `ducky/hot/add.py` · `ducky/session_distill.py` |
| **删得干净、删错能恢复** | 单条删除同时清掉：向量与全文索引、它对应的原话行、由它派生的事实、以被删原话为来源的会话精华（同样文字的其他独立记忆不受牵连）。删除前留墓碑快照，可用 `POST /tombstone/restore` 恢复；快照默认 30 天后擦除正文（`AIDUMEI_TOMBSTONE_TTL_DAYS`，0 = 不过期）。删除结果如实返回 `committed` / `partial` / `not_found` | `ducky/wal_engine.py` · `ducky/tombstone.py` |
| **一条命令接宿主** | `python scripts/connect_host.py`：备份宿主配置 → 幂等写入三条钩子（命令直接指向仓库脚本，不拷贝，升级不留旧文件）→ 用刚接好的钩子跑一轮合成对话（写入暗号 → 新会话召回 → 删除测试记忆）。首行固定输出 `服务 ✅/❌ · 宿主接入 ✅/❌` | `scripts/connect_host.py` |
| **写线断了会报警** | 只接读线不接写线时一切看似正常、新话却全丢——这是本项目最贵的一次事故。现在 `/health` 的 `host_wiring` 记录最近一次宿主写入，服务起来 30 分钟没收到会进 warnings | `ducky/host_wiring.py` |
| **心图体检** | `GET /graph/checkup` 只读地回答三件事：哪些话存了几份、哪些已过期、同一主题当前有几个不同的值。处置仍走 `/delete`、`/update`，删除照样进墓碑 | `ducky/memory_checkup.py` |
| **本地档零外呼** | `AIDUMEI_ENGINE_MODE=local` 时 LLM、云嵌入、重排一律不发请求，有测试把 HTTP 发送换成「一调用就失败」来验证 | `ducky/engine_mode.py` · `tests/test_v20_2_4_audit_remediation.py` |
| **设置页测试连接** | `POST /config/test` 对各模型段做一次最小真实调用，不回显密钥；局域网模型机需经 `AIDUMEI_MODEL_ENDPOINT_ALLOW` 显式放行，云元数据地址永远拦截 | `ducky/routes_config.py` · `ducky/config_probe.py` |
| **控制台** | `http://127.0.0.1:8767/ui`：浏览/搜索记忆、健康与挡位、心图体检、记忆星云、模型设置；零构建纯静态 | `frontend/` |

## 快速开始

### 方式一：让 Agent 部署（推荐）

把这句话发给你的 AI Agent：

> 你现在是部署工程师。请按 <https://github.com/monkey2jack/aiduMEI> 的 `prompts/install.txt` 全文（14 行正典）在我这台机器上部署 aiduMEI，每步自己验证，不许假装成功。

正典带它走完：环境检查 → 装依赖 → 选挡位 → 配 Key（向你索要，不编造）→ 起服务 → 真实写入/召回验证 → `connect_host.py` 接宿主 → 装定时任务与首次备份 → 出报告。Agent 侧的完整说明在 [🤖 Agent Guide](AGENTS.md)。

### 方式二：手动

需要 Python 3.10–3.12（推荐 3.12）。

```bash
git clone https://github.com/monkey2jack/aiduMEI.git && cd aiduMEI
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp mem0_config_local.json.example mem0_config_local.json   # 模型配置：填 LLM 与 Embedding 的地址和 Key（本地档可不填）
cp .env.example .env                                         # 填 AIDUMEM_API_TOKEN
python api_server.py                                         # → http://127.0.0.1:8767
```

挡位三选一（`AIDUMEI_ENGINE_MODE=auto` 为默认）：

| 挡位 | 需要什么 | 效果 |
|---|---|---|
| `cloud` | 云端 LLM + Embedding Key | 最轻；云端断了召回如实报 `degraded` |
| `auto`（默认） | Key + `pip install .[local-embed]` + `python scripts/fetch_local_embed_model.py` | 云优先；native 内核下嵌入挂了退回全文检索（本地向量备胎只在 mem0 内核下有），恢复后换回 |
| `local` | 不需要 Key | 零 token、零外呼；默认 native 内核，只走全文检索；会话精华需要 LLM |

换挡要重启服务。

### 接宿主并验证

```bash
python scripts/connect_host.py            # 识别 + 接入 + 当场验收；--dry-run 只看要改什么，--check 只验收
python scripts/e2e_smoke.py --json        # 写入唯一暗号 → 新请求召回 → 清理；status=PASS 且 0 warnings 才算通过
curl -s -H "Authorization: Bearer $AIDUMEM_API_TOKEN" http://127.0.0.1:8767/health | jq '.health_status, .degraded, .probes.runtime_paths'
```

`/health` 先看三个数：`health_status` 为 `ok`、`degraded` 解释了每个不可用组件、`probes.runtime_paths.data_dir_writable` 为 true。只看 `/health` 不够——它全绿时写线也可能没接上；真实用过几轮后可再跑 `python3 scripts/check_ingest_wiring.py --require-judgment` 复核（凭据走环境变量或 `.env`，别写成 `--token` 参数，命令行对本机其他用户 `ps` 可见）。

`connect_host.py` 不认识的宿主（如 Cursor），按 [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md) 手工接，再跑 `python scripts/agent_integration_check.py`。

### 容器部署（Docker Compose）

`docker compose up -d` 之前三件事缺一不可：

1. **凭据**：在 `docker-compose.yml` 旁的 `.env` 写 `AIDUMEM_API_TOKEN=<随机长串>`。容器内绑定 `0.0.0.0`，无凭据的非回环绑定会被拒绝启动；compose 用 `${AIDUMEM_API_TOKEN:?…}` 引用，缺了当场报错。compose 只拿 `.env` 做插值，其余键不会进容器——要改内核或其他配置，加进 compose 的 `environment`。
2. **目录属主**：`mkdir -p data logs && sudo chown -R 10001:10001 data logs`（容器以 uid 10001 运行；漏了的症状是第一次写入报 `unable to open database file`）。
3. **模型配置放进数据目录**：`cp mem0_config_local.json.example data/mem0_config_local.json` 后填 Key（容器读 `/app/data/mem0_config_local.json`）。（仅 mem0 内核）向量库路径 `vector_store.config.path` 要留在 `./data` 里，否则重建容器即丢。

起来后 `docker compose ps` 应为 `healthy`（容器内探 `/livez`），再跑 `docker compose exec aidumem python scripts/e2e_smoke.py --json`。托管平台见 [docs/DEPLOY_DOCKHOLD.md](docs/DEPLOY_DOCKHOLD.md)。

## 日常运维

| 要做什么 | 命令 |
|---|---|
| 看健康 | `/health`（上面三个数）；`python scripts/report.py --json`（退出码 0 健康 / 2 警告 / 3 降级） |
| 装定时任务 | `bash scripts/update_crontab.sh install`，再用 `bash scripts/update_crontab.sh --installed` 逐项核对 |
| 备份 | `bash scripts/backup_gate.sh create <名字>` → `bash scripts/backup_gate.sh verify <备份目录>`（整个数据目录，含 `memory.db`，SQLite 在线快照 + 校验和） |
| 恢复（隔离演练） | `bash scripts/restore_gate.sh --dry-run <备份目录>`；正式恢复只往**不存在的新目录**写，详见 [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) |
| 恢复误删的单条记忆 | `GET /tombstones` 查墓碑 → `POST /tombstone/restore` |
| native 库体检（只读） | `python scripts/native_preflight.py --data-dir <数据目录>` |

**备份是明文**：所有记忆正文与数据目录里的凭据都在备份里，按活数据同等保管；不内置加密，建议持盘机器全盘加密。

**升级**：

1. 先备份并验证；跑 `bash scripts/pre-upgrade-check.sh`。
2. `git pull` → `pip install -r requirements.txt` → 重启服务。
3. 重新跑 `python scripts/connect_host.py`（幂等；钩子指向仓库脚本，升级后当场验收一次）。早期手工拷贝过钩子的 Hermes 部署，跑 `python3 scripts/check_hook_deployment.py`——它按 `~/.hermes/config.yaml` 声明的真实路径比对，退出码 0 才算部署到位。
4. `python scripts/e2e_smoke.py --json` → `bash scripts/post-upgrade-check.sh`。

**从 mem0 内核迁到 native**（老部署升级到 f0.7 时做一次；没迁移前服务会自动留在 mem0 内核，日志有提示）：先 `pip install -r requirements-mem0.txt`，服务停着时 `python scripts/migrate_to_native.py --data-dir <旧数据目录> --out <新 memory.db>`（源库只读、可重复执行、带对账报告；缺向量的行可加 `--embed-missing` 补，会外呼），把生成的 `memory.db` 放进数据目录后重启即用 native（也可显式设 `AIDUMEI_MEMORY_KERNEL=native`）。

**回滚**：代码回滚只还原代码、钩子和它自己的配置改动，**不要把旧备份数据盖到快照之后的新写入上**（[docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md)）。内核从 native 退回 mem0 前，先 `pip install -r requirements-mem0.txt`，停服务跑 `python scripts/native_to_mem0_resync.py --data-dir <数据目录> --since <切到 native 的时刻>`（默认只预览，`--apply` 才写），把 native 期间的写、改、删补回旧存储，再设 `AIDUMEI_MEMORY_KERNEL=mem0`。

## 关键配置

`.env` 放在仓库根目录，启动时读入（已存在的环境变量优先）。全量登记册是 `ducky/env_registry.py`，完整注释见 `.env.example`。

| 变量 | 作用 | 默认 |
|---|---|---|
| `AIDUMEM_API_TOKEN` | API 鉴权令牌；非回环监听必须配 | 空 = 只许回环 |
| `AIDUMEM_UI_PASSWORD` | 控制台口令（PBKDF2 存储） | 空 |
| `AIDUMEM_DATA_DIR` | 数据目录 | 仓库下 `data/` |
| `AIDUMEM_API_PORT` | REST 端口 | 8767 |
| `AIDUMEI_ENGINE_MODE` | 挡位 `cloud` / `auto` / `local` | `auto` |
| `AIDUMEI_MEMORY_KERNEL` | 记忆内核 `native` / `mem0` / `shadow`；未知值启动即报错，不静默回退 | `native`（老 mem0 部署未迁移时自动 `mem0`） |
| `AIDUMEI_TOMBSTONE_TTL_DAYS` | 墓碑快照保留天数，到期擦正文；0 = 不过期 | 30 |
| `AIDUMEI_MODEL_ENDPOINT_ALLOW` | 放行局域网/tailnet 模型机（逗号分隔主机名或 IP） | 空 |
| `AIDUMEI_RERANKER_API_KEY` | 重排密钥，优先于配置文件 | 空 |
| `AIDUMEI_CALLER_BINDINGS` | 凭据指纹 → 可代表的主体名单 | 空 |
| `AIDUMEI_CALLER_BINDING_MODE` | `strict` / `permissive` / `off` | `off` |
| `AIDUMEI_MCP_DANGEROUS_TOOLS` | 设 1 才注册 MCP 的 `mem_update` / `mem_delete` | 关 |
| `AIDUMEI_SKIP_PLATFORMS` | 这些宿主平台的会话不写记忆、不提炼（Hermes 钩子与插件）；置空串关闭 | `cron,subagent` |
| `AIDUMEI_INJECT_DATE` | 注入的记忆带不带时间 `day` / `minute` / `off`（钩子侧） | `day` |

新变量一律 `AIDUMEI_` 前缀；`AIDUMEM_` 为旧前缀，存量不迁移。模型地址、密钥在 `mem0_config_local.json`（文件名沿用旧称，native 内核也从这里读 embedding 配置），也可在控制台设置页修改并「测试连接」；重排配置一次后，符合条件的检索自动使用，详见 [docs/DECISION.md](docs/DECISION.md) 与 `.env.example`。

## 接入面

| 宿主 / 方式 | 怎么接 |
|---|---|
| Claude Code | `connect_host.py` 写 `~/.claude/settings.json`：`UserPromptSubmit` 读线 / `Stop` 写线 / `SessionEnd` 萃取线（`integrations/claude-code/`） |
| Codex CLI（≥ 0.163） | `connect_host.py` 写 `~/.codex/hooks.json`，三线同上（`integrations/codex/`）；非托管钩子需在 Codex 里信任一次 |
| Hermes Agent | `connect_host.py` 软链插件 `integrations/hermes-plugin/aidumem` 并设 `memory.provider aidumem` |
| Cursor 等其他宿主 | 按 [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md) 手工接 |
| HTTP API | REST `:8767`：`/add` `/search` `/update` `/delete` `/session/distill` `/tombstones` `/graph/*` `/api/core-memory` 等；写接口带 `caller_user_id` |
| MCP | 见下节 |

## MCP Server（6 工具 · 默认端口 8766）

`mcp_server.py` 用官方 SDK `mcp==1.30.0`（已在 `requirements.txt`），stdio / HTTP 双传输，默认端口 8766。工具：`mem_add`、`mem_search`、`mem_health`、`core_memory_list`，以及默认**不注册**的 `mem_update`、`mem_delete`（设 `AIDUMEI_MCP_DANGEROUS_TOOLS=1` 才开）。

鉴权：非回环绑定必须配 `AIDUMEM_API_TOKEN`，否则拒绝启动；确需无凭据公网暴露才设 `AIDUMEM_ALLOW_INSECURE_PUBLIC=1`，且还须设 `AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH=<监听地址>`（值必须逐字等于实际监听地址），REST 同理。同一连接对同一工具、同一参数连续失败会被循环保护拦下（`AIDUMEI_MCP_LOOP_GUARD`，默认 5 次/60 秒/冷却 30 秒，单进程内有效）。

## 🔐 安全模型

- **凭据**：Bearer 令牌 `AIDUMEM_API_TOKEN`；控制台口令 PBKDF2；默认只监听回环。启动后先查三个数（见上）；不带凭据时 `/health` 探针脱敏并附 `_redacted` 说明。
- **单主人**：服务只认一个 API token，`caller_user_id` 是客户端自报的。**不支持互不信任的多用户隔离**，一个服务给一个主人（或彼此信任的 agent）用；需要按凭据限制主体时配 `AIDUMEI_CALLER_BINDINGS` + `AIDUMEI_CALLER_BINDING_MODE=strict`。
- **出站**：模型地址默认只收 https 公网地址或本机，私网与云元数据地址拦截（局域网模型机可用 `AIDUMEI_MODEL_ENDPOINT_ALLOW` 放行），模型外呼不跟随重定向；各云出口发送什么字段见 [docs/OUTBOUND_CHANNELS.md](docs/OUTBOUND_CHANNELS.md)。
- **注入防护**：召回内容注入宿主前做中和与围栏；破坏性 MCP 工具默认关闭；`delete_all` 必须显式 `confirm=true`。
- 外部审计逐条留痕：[docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md)、`SECURITY.md`。

## 已知例外与本版不覆盖

| # | 例外 | 说明 |
|---|------|------|
| 1 | mem0 旧内核仍保留为可选 | 代码里 mem0 / shadow 内核、迁移与回滚脚本还在，需另装 `requirements-mem0.txt`；默认 native 不装不加载。模型配置文件仍沿用旧名 `mem0_config_local.json`；墓碑恢复写回的 FTS5 文本字段，装了 mem0 时借用它的词形化，没装时直接用原文（`ducky/tombstone.py`） |
| 2 | 「单库」指记忆内核，不是整个服务只有一个文件 | `memory.db` 存记忆本体；确定性抽取的事实与核心记忆仍在 `facts.db`（`ducky/memory_kernel/impl_native.py` 注明事实层单一真源是 facts.db），另有 mutation journal 等辅助库，备份按整个数据目录做 |
| 3 | native 没有本地向量备胎 | 嵌入接口挂了或在本地档时退回 FTS5 trigram 全文检索，语义改写类问法召回会变弱；`ducky/engine.py` 中 native 不走 auto 挡的本地备胎腿。native 只支持 OpenAI 兼容 embedding |
| 4 | 租户隔离是按租户收窄可见性 | 不是互不信任客户之间的强隔离层；域契约见 `ducky/bank_contract.py` |
| 5 | 本地嵌入模型须部署期下载 | 运行时零网络，`ducky/local_embed.py` 强制 `HF_HUB_OFFLINE=1`；取模后按 `scripts/local_embed_model_sha256.json` 逐文件校验 |
| 6 | 写入不保证跨存储原子 | 有写入日志与校验链，不确定的写入需核验修复，不承诺 exactly-once；哈希链不是签名 |
| 7 | 平台 | 全量测试按 Linux/macOS 维护；Windows 可启动服务，但跨进程写保护、备份闸门与运维脚本只保证 POSIX |

## 测试与质量

**质量怎么量**：不追公开跑分，不宣称任何基准名次。质量以**真实使用回归集 v3** 为准（171 条：事实回忆、更新、删除、时间、拒答五类，外加 20 条 Claude Code 宿主回放），零 token、本地一分钟内跑完；`scripts/push_gate.sh` 对 mem0 与 native 两个内核各跑一遍，任一类得分低于基线即拦下合并。真实集取自维护者自己的使用记录（已脱敏），**不随公开仓分发**；公开仓用 `benchmarks/build_public_regression.py` 生成同结构的虚构集，两套分数不可互相比较。跑法：`python -m benchmarks.regression --baseline benchmarks/regression_v3_baseline_native.json`（加 `AIDUMEI_MEMORY_KERNEL=native`）。

**测试层级如实说明**
> 读表口径：每行「通过 · 跳过」相加等于**该形态、该日期**下 `pytest --collect-only` 的收集数；不同日期的行分母可以不同（树在长），以各行括注的日期为准。跳过数按「轴」解释（见 [docs/TESTING.md](docs/TESTING.md)），不是失败。

| 维度 | 现状 |
|------|------|
| 用例总数 | **3907**（`pytest --collect-only` 实测，2026-10-30，f0.7.1 树）＝ **行为用例 3279 + 脚本/钩子行为 482 + 守卫用例 146**；三桶口径见 `scripts/count_test_kinds.py`，可一键复算 |
| 独立开发机 | 3895 通过 · **12 跳过** —— f0.7 按收集数与缺席轴推导的验收目标（Python 3.12，仅缺 Hermes 宿主源码） |
| 基础安装路径 | 1821 通过 · **25 跳过** —— 只装 `requirements.txt` + `requirements-dev.txt`（**2026-09-09 生产机干净 venv 实测**，旧树）。f0.7 起全量套件同时测 mem0 旧内核，需加装 `requirements-mem0.txt`；只装 `requirements.txt` 的默认 native 路径改由从零安装验收与 CI 裸装冒烟覆盖（2026-10-29 实验机：安装、健康、写入、召回、删除、恢复通过） |
| 生产机沙箱 | 1967 通过 · **26 跳过** —— **2026-09-11 生产机实测**（旧树 `de09794`，宿主源码在场、不带 `.env`、无 ruff/mcp/fastembed） |
| 全轴齐备 | 1844 通过 · **1 跳过** —— **2026-09-09 生产机实测**（旧树，工具、extras、宿主、模型缓存与公开 LoCoMo 数据集齐备） |
| 层级 | 以模块级单元测试 + 源码级守卫断言为主，`TestClient` 驱动的接口测试为辅 |
| 未覆盖 | 真实供应商、宿主 CLI 与生产性能另列实机证据，不由用例数证明；语句覆盖率未重新测量 |

```bash
pip install -r requirements.txt -r requirements-mem0.txt -r requirements-dev.txt
pytest tests/                                          # 全量回归
python -m compileall ducky api_server.py mcp_server.py # 编译检查
```

> **为什么要把 3895 和 1967 都写出来**：3895 是本树仅缺 Hermes 宿主 ×12 时的预期通过数，不是实测成绩；1967 是生产机独立沙箱 2026-09-11 在旧树上的实测——两者跳过轴不同，数字必须与环境、日期和测试树一起读。

> **这 12 条跳过自己就能验**：十四条跳过轴全部登记在册（[docs/TESTING.md](docs/TESTING.md)），`HERMES_SRC` 三态可控、两个方向都能复现：
>
> ```bash
> pip install "mcp==1.30.0" ruff nltk regex numpy fastembed && python scripts/fetch_local_embed_model.py
> pytest tests/ -q -rs | tail -1                                 # 无宿主：3895 passed, 12 skipped
> HERMES_SRC=/path/to/hermes-agent pytest tests/ -q | tail -1    # 补齐宿主及其余可选轴后目标——有宿主：3907 passed
> HERMES_SRC=none pytest tests/ -q -rs | tail -1                 # 装了宿主也强制关掉，照旧 3895 passed, 12 skipped
> ```
>
> 「有宿主」那行要十四条轴同时齐备才拿得到。生产机独立沙箱裸跑（宿主源码在场、不带 `.env`），旧树上实测跑出来是 1967 passed、26 skipped（2026-09-11，`de09794`）。
>
> | 跳过轴 | 门控用例数 | 位点 |
> |--------|-----------|------|
> | 宿主 Hermes 源码 | 12 | `tests/test_hermes_plugin.py` 整份 |
> | git 工作区 | 1 | `tests/test_v20_brand_policy.py` |
> | `scripts/backup_gate.sh` + POSIX shell | 8 | `tests/test_v19_4_1_backup_gate.py` 整份 |
> | WAL POSIX 真实锁与目录 fsync | 5 | `tests/test_f03pp_wal_integrity.py` 的 POSIX 专项 |
> | `qdrant_client` 已安装 | 1 | `tests/test_v20_vector_bank_contract.py` |
> | LoCoMo 数据集已就位 | 1 | `tests/test_v20_locomo_official.py` |
> | `regex` 已安装 | 1 | `tests/test_v20_locomo_official.py` |
> | `numpy` 已安装 | 1 | `tests/test_v20_locomo_official.py` |
> | `nltk` 已安装 | 13 | `tests/test_v20_locomo_official.py` 与 `tests/test_v20_benchmarks.py` |
> | `git` 可执行文件在场 | 6 | `tests/test_v20_gitignore_guard.py` 整份 |
> | `mem0ai` 已安装 | 20 | `tests/test_v20_mem0_patch_layer.py` 整份 |
> | `fastembed` 已安装 | 1 | `tests/test_v20_2_autoshift.py` |
> | `ruff` 已安装 | 3 | 静态规则守卫 F821/F811/F841 |
> | `mcp` extra 已安装 | 9 | MCP 导入面、鉴权、SSE 传输与检索 session 透传 |

## 资源占用：f0.7 对比 f0.6

同一台 4 核 8G 云主机（2026-10-29），两版各从零安装、各跑两遍，取两遍范围；两版都开着鉴权门禁。嵌入/LLM 用本地桩服务（固定 1024 维向量），只量 aiduMEI 自己和它的向量库，不含模型推理。f0.6 按当时的生产形态测：mem0 内核 + Qdrant 容器（v1.20.0）；f0.7 为默认 native 内核。

**开箱（装完、启动后空载）**

| 项 | f0.6（mem0 + Qdrant） | f0.7（native） | 变化 |
|---|---|---|---|
| 直接依赖 / 装出的包 | 12 / 55 个 | 10 / 36 个 | 少 19 个包 |
| Python 环境体积 | 174 MB | 98 MB | −44% |
| 另需 Qdrant 镜像 | 276 MB | 不需要 | −100% |
| 安装总占用 | 约 450 MB | 约 98 MB | −78% |
| 常驻进程 | 2 个（服务 + Qdrant） | 1 个 | |
| 空载内存 | 179 MB | 63 MB | −65% |
| 启动到可用 | 1.8 s | 0.6 s | 快 3 倍 |

**使用（连续写入 2000 条，再检索 300 次）**

| 项 | f0.6（mem0 + Qdrant） | f0.7（native） | 变化 |
|---|---|---|---|
| 内存（服务 + 向量库） | 207 MB | 112–113 MB | −46% |
| 服务进程峰值 | 171–176 MB | 127–128 MB | −27% |
| 磁盘（数据库合计） | 46 MB（SQLite 25 + Qdrant 21） | 40 MB（单库） | −13% |
| 写入 P50 / P95 | 76 / 102–109 ms | 63–64 / 97–99 ms | 略快 |
| 检索 P50 / P95 | 17–19 / 24–26 ms | 30–34 / 38–42 ms | 慢约 13–15 ms |

说明：
- 检索变慢是已知取舍：native 用单库内的精确向量比对，换掉了 Qdrant 的近似索引；在几千条的个人规模下仍在 45 ms 以内。
- 生产实际内存会高于此表：真实嵌入客户端、更大的记忆库和后台任务都会占内存。表格用于两版之间的公平对比。
- 本地档（`local`，带本地嵌入模型）未重测；这一档会额外多出模型运行库与权重的内存。
- 复现：`python scripts/slim_bench.py --code <代码目录> --venv <venv> --label f0.7`；测旧版加 `--qdrant --kernel mem0`（需 Docker）。

## 文档索引

| 主题 | 文档 |
|---|---|
| Agent 部署与运维入口 | [AGENTS.md](AGENTS.md) · [prompts/install.txt](prompts/install.txt) |
| 宿主接入 | [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md) · [integrations/INTEGRATION_GUIDE.md](integrations/INTEGRATION_GUIDE.md) |
| 健康与排障 | [docs/HEALTH.md](docs/HEALTH.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| 运维、备份、升级、回滚 | [docs/OPERATIONS.md](docs/OPERATIONS.md) · [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) |
| 出站与安全 | [docs/OUTBOUND_CHANNELS.md](docs/OUTBOUND_CHANNELS.md) · [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md) |
| 决策模型（可选） | [docs/DECISION.md](docs/DECISION.md) |
| 测试口径 | [docs/TESTING.md](docs/TESTING.md) · [docs/BENCHMARKING-POSTURE.md](docs/BENCHMARKING-POSTURE.md) |
| 版本规则与历史 | [docs/VERSIONING.md](docs/VERSIONING.md) · [CHANGELOG.md](CHANGELOG.md) |

## 仓库结构

```text
aiduMEI/
├── api_server.py            # 主入口：REST API + /ui 控制台
├── mcp_server.py            # MCP Server（:8766）
├── ducky/                   # 业务代码
│   └── memory_kernel/       #   内核可换层：native（单库）/ mem0（旧）/ shadow
├── frontend/                # 控制台（零构建纯静态）
├── integrations/            # 宿主钩子与插件（claude-code / codex / hermes-plugin / cursor-hook）
├── scripts/                 # connect_host / e2e_smoke / backup_gate / migrate_to_native 等
├── benchmarks/              # 真实使用回归集运行器与基线
├── prompts/install.txt      # 一键部署正典
├── docs/                    # 运维、健康、备份、接入、测试文档
└── tests/                   # pytest 回归
```

## License

MIT — 详见 [LICENSE](LICENSE)。

<p align="center">
  <sub>aiduMEI ☤ 爱嘟优忆思（品牌演进 aiduMEM → aiduMEI，曾用名 aiduMEM / duMem 保留在历史版本与文档中）｜Powered by monkey²</sub>
</p>

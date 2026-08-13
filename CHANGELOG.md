# aiduMEI 版本演进史

> 从 mem0 裸壳到五脉架构，再到 Pantheon 万神殿与 Aegis 神盾，经 Zeus 多模态感知，直至 v19.0 Athena 从记忆到智慧。

---

## v19.1.0 — Athena 雅典娜 · 审计修复版（2026-08-13）

> 社区网友对 v19.0 做了 file:line 级全量代码审计，指出 26 项问题。本版逐条独立复核后全部修复，并对少数夸大的营销措辞做诚信对齐。感谢审计者。

### 🔴 数据安全
- **联邦跨 Agent 隔离**：facts 唯一索引由 `(category, fact_key)` 升级为 `(agent_id, category, fact_key)`，`ON CONFLICT` 目标同步带 agent_id。此前 Agent B 写同 key 会静默覆盖 A 的记忆且仍记 A 名下。存量库自动安全重建；新增跨-agent 回归测试。
- **联邦 UPDATE 不再重置衰减时钟**：0.70–0.85 相似度更新不再刷新 `recorded_at/decay_at`，与 merge 分支语义对齐。

### 🔴 启动与主链
- **全新部署开箱可用**：联邦 schema 迁移前置核心建表，修 fresh clone 时 `agents/federation_broadcast` 表缺失、联邦端点全返 `no such table`。
- **写入主链接线**：正常 `/add` 补齐 salience 登记 + FTS 索引 + 六型写时分类。此前正常新增的记忆全文搜不到、热度不累计。

### 🔴 Athena 断链修复
- **技能人工审批**：新增 `POST /crystals/approve`，此前 draft 永远转不了正。
- **conflict REGEXP 注册**：给 SQLite 连接注册 `REGEXP`，冲突消解热路径此前必抛 `no such function` 静默空转。
- **self_edit 相似度门控**：启用 `_CANDIDATE_SIM_FLOOR`，避免每次 `/add` 同步烧 LLM 阻塞写入。

### 🔴/🟠 端点与脚本
- `/metrics` 上线（运行时指标）；`/gate` 上线（Tahoe-Gate 相关性闸门，此前零调用）。
- `mem_search_deep` POST→GET 修 405；`/search/deep` 改关键词检索，不再依赖从未创建的 `facts_fts`。
- `/scene` 建表 + 结果落库修开箱 500；`restore_backup.py` 端点 `/api/memory/add`→`/add` 修 404。
- SETTINGS 保存 `PUT`→`POST` 且检查 `r.ok`，不再对失败假报「已保存」；升级脚本移除幽灵文件引用、mem0ai 基线对齐 `2.0.18`。

### 🟡 缺陷修复
- `session_unpin` 判空逻辑写反修正；`session_search` 的 `context_used` 恒真修正。
- `workspace.db` 改走 `AIDUMEM_DATA_DIR`；vision 失败字符串不再落库；autodream 不再物理改写原文（仅归档 + `autodream_log` 溯源）。

### 🟠 诚信与一致性
- **版本号统一**：`version.py` 真相源升 `19.1.0`，`mcp_server.py`/`manifest.json` 全部从真相源取值。
- **manifest 可配置项真读取**：`salience_half_life_days`/`salience_floor`/`consolidation_interval_hours` 由环境变量 > manifest > fallback 实际读取。
- **卖点措辞对齐实现**：移除「Token 降低 100 倍 / 10ms→1ms」未经基准验证的表述；「零依赖前端」标注 MAP 面板依赖 ECharts CDN。

---

## v19.0.0 — Athena 雅典娜（2026-08-13）

> 从记忆到智慧。前代 Zeus 打通「记什么、怎么记、怎么找回来」；雅典娜从宙斯头颅中全副武装诞生，补上认知闭环的后半程——**记忆存下来之后，Agent 如何主动反思、自我修正、越用越精炼、把经验长成技能，并拥有稳定的人格底座**。记忆不再只增不减，而是会自省、会收敛、会进化。

### 核心新特性

**🔮 Reflect 主动反思（P0-3 · 借鉴 Hindsight）**
- 新增 `ducky.reflect` 反思引擎：定期/触发式回顾记忆，提炼模式、关系、预测、矛盾、知识缺口为洞察
- 洞察落库为一等公民 `reflections`，可列表查询、可注入上下文；同一洞察按 content 哈希幂等落库
- 后台每 6 小时自动反思（`AIDUMEM_REFLECT_INTERVAL_HOURS` 可调，`AIDUMEM_REFLECT_ENABLED=false` 关闭）
- **会话结束自动触发**：`/session/end` 后台拉起 `run_reflect(source="session_end")`（`AIDUMEM_REFLECT_ON_SESSION_END` 开关）
- 新增路由 `POST /reflect`、`GET /reflect/list`、`GET /reflect/context`；降级友好，LLM 不可用返回空洞察不抛异常

**✏️ 记忆去重自编辑（P0-2 · 借鉴 Mem0）**
- 新增 `ducky.self_edit`：写入前用 LLM 判断新记忆与既有记忆是「重复 / 冲突 / 全新」，重复合并、冲突并存标注置信度
- LLM 语义判重先行，`Layer1` Jaccard 零成本兜底；LLM 不可用无缝回退，向后完全兼容
- 每次合并/冲突更新快照进 `memory_edits` 表，`POST /self-edit/rollback` 一键回滚；新增 `GET /self-edit/edits`

**🗂️ 记忆类型分离（P1-1 · 借鉴 Hindsight 四网络）**
- 新增 `ducky.memory_types`：六种认知类型显式管理——FACTS / PREFERENCES / EXPERIENCES / OBSERVATIONS / REFLECTIONS / DECISIONS
- 不推翻现有存储，加一层类型标签与查询视图；新增 `/memory/types`、`/memory/types/query`、`/memory/types/backfill`、`/memory/types/reset`

**🌱 自动 Skill 生长 + 精炼淘汰（P1-2 · 借鉴 ReMe/MemU）**
- 新增 `ducky.skill_growth`：任务轨迹回放 → 步骤提取 → LLM 生成 SKILL.md 草稿 → 人工 approve → 归档
- 技能复用打点 `record_skill_use`（成功/失败计数）；`prune_low_utility_skills` 淘汰低效用技能（成功率 < 34% 标记 archived，不物理删除）
- 新增 `POST /skill/grow`、`GET /skill/drafts`、`POST /crystals/use`、`POST /crystals/prune`
- 治理铁律沿用 Mímir：LLM 只能建议草稿（`status='draft'`），不能自动 commit

**🧬 记忆递归精炼（P1-3 · 借鉴 SimpleMem）**
- 新增 `ducky.refine_memory`：后台把相关多条碎记忆递归合并为高层抽象，对抗记忆熵增
- 与 self_edit 分工：自编辑管写入时 1 对 1 判重，精炼管后台多对 1 聚类压缩
- 精炼产物写入 `refined_memories`，原记忆 soft-superseded 不物理删除，可一键回滚；新增 `/memory/refine`、`/memory/refine/apply`、`/memory/refine/rollback`、`/memory/refinements`

**🎭 人格记忆基座 · Persona Memory Layer（借鉴 MemoryForge）**
- 新增 `ducky.persona_memory`：把一句话人设展开成可按情境检索的自传体记忆库，L（生平）/ G（成长）/ E（情节）三层
- 双模式：`synthesis`（合成，面向虚构角色，自动生成三层）/ `grounded`（落地，面向真实用户，从已有记忆归纳不虚构）
- 与运营记忆双层并行，版本化可回滚；新增 `/persona/build`、`/persona/banks`、`/persona/detail`、`/persona/retrieve`、`/persona/context`、`/persona/rollback`
- MCP 新增 `persona_build` / `persona_retrieve` / `persona_banks` 三工具（MCP 工具总数 38 → 41）

**🕰️ 双时间轴记忆 + 时间感知检索（P0-1 / P0-4）**
- P0-1：`/add` 自动写入 `valid_from` / `valid_to` / `recorded_at` 双时间轴；`created_at → recorded_at → valid_from` 三级时间源回退
- P0-4：混合检索多信号加权融合——向量 + BM25 + 时效衰减 + 可靠性 + 热度；时间衰减率 `λ` 环境变量可调

### 部署与工程

- mem0 内核锁定 `2.0.18`、`qdrant-client 1.18.0`，与生产基座对齐
- 向量库嵌入式 on-disk（`path: ./data/qdrant`），无独立服务/容器/端口
- 实测运行内存约 210 MB RSS（单进程），2 核足够，闲时 CPU < 1%；仅 9 个顶层依赖
- SQLite `ALTER TABLE ADD COLUMN` 幂等迁移覆盖两代旧 `skill_crystals` schema，老库平滑升级零数据丢失
- `_normalize_user_id` 历史 user_id 映射改由环境变量 `AIDUMEM_LEGACY_USER_IDS` 注入，仓库零硬编码身份

### 测试

- 新增 P0/P1/persona/session-end 全套单测：81 passed（含 test_p0_upgrades / test_p1_memory_types / test_p1_refine_memory / test_p1_skill_growth / test_p1_skill_refinement / test_persona_memory / test_session_end_reflect）

---

## v18.3.0 — Zeus 宙斯（2026-08-11）

> 多模态感知纪元：无损秒级升级机制 + 多模态视觉记忆 + Obsidian 双链联动。
>
> 洁净度说明：开源发布时已移除残留的内部引用（网关命名 / 部署环境描述），纯文档级清理，无功能变更。

### 核心新特性

**⚡ 无损秒级平滑升级 (Fast-Update)**
- 引入基于 `PRAGMA user_version` 的 schema 版本化机制（`CURRENT_SCHEMA_VERSION = 2`）
- 新增 `apply_migrations()`：SQLite `ALTER TABLE ADD COLUMN` 毫秒级增量补丁，代码更新与数据重构彻底解耦
- 老库自动检测并平滑迁移（v1 → v2 增加 `media_url` / `vision_caption` 字段），数据零丢失
- 配套《Fast-Update SOP》运维文档：3 步完成版本升级，大版本可秒级回滚

**🖼️ 多模态视觉记忆 (Vision)**
- `/add` 原生支持 `media_url` / `image_url`，后端自动调用 OpenAI 兼容 Vision API 生成 `vision_caption`
- 支持 base64 / data URI / 远程 URL 三种图片输入
- 独立 `vision` 配置段（fallback 到 `llm` 段），Vision 用量自动追踪
- 前端 VAULT 渲染图片缩略图、PULSE 统计多模态数据、SETTINGS 展示 Vision 模型配置

**🔗 Obsidian 双链联动 (Obsidian Bi-directional Links)**
- 新增 `POST /api/obsidian/sync`：接收 Obsidian 笔记推送，解析 `[[Wikilink]]` 双链语法
- 双链词自动沉淀为实体图谱节点（`entities` 表 `obsidian_node` 类型），打通 TreeMemory 拓扑
- 模块独立开关（`_features.obsidian` / `_features.vision` / `_features.fast_update`），可随时启停

**🔐 控制台增强**
- SETTINGS 新增登录密码修改（`POST /config/password`，写入 .env 重启生效）
- 修复 RECALL 显式搜索被相关性闸门误拦截的问题：`/search` 直走 Workspace → Hybrid 混合召回
- 用户 ID 规范化：历史命名统一映射到 `default`，老数据可被新查询命中

---

## v18.1.0 — Zeus（2026-08-07）

> EvolveMem 检索自进化纪元：融合 SimpleMem 核心思想，构建基于质量反馈的动态权重闭环。

### 核心新特性

**📈 EvolveMem 检索自进化**
- 新增 `ducky.evolve_mem` 核心引擎，支持动态反馈与权重衰减/提权
- 新增 `POST /evolve/feedback`，允许用户传入 `useful` / `useless` / `correction`，实时微调 `salience`
- 新增 `GET /evolve/report` 进化统计面板（召回率、有效性打分、动态调整历史）
- 新增后台自动进化线程：每 6 小时计算衰减/提权，自动沉淀（>5次命中且分数>0.65的高频词条获得提权）
- 将 EvolveMem 质量打点钩子无缝植入 `recall_funnel` 漏斗末端，完成搜索闭环

**🛠️ MCP 工具持续扩容**
- 将 MCP 的能力由 36 提升至 38
- 新增 `evolve_feedback` 和 `evolve_report` 本地代理

**🧹 全局质量审计**
- 完成项目内 100% 裸 `except:` 的审计重构（改为 `except Exception:` 阻断隐患）
- 修复 `ducky/hot/health.py` 中遗漏的环境变量探针

### 三大借鉴圆满收官
通过 v18.0 (Zeus) 和 v18.1 (Zeus)，全面完成了用户交代的“吸星大法”：

---

## v18.0.0 — Zeus 宙斯（2026-08-07）

> 吸星大法纪元：吸收全网 Top 5 AI 记忆系统精华，跨代架构融合升级。

### 核心新特性

**⚡ Raw Drawer 原味抽屉（吸收 MemPalace Verbatim Storage）**
- 新增 `POST /add/raw` 端点，零 LLM 直存原始文本
- FTS5 全文索引 + Qdrant 向量 + facts 登记，三路并行
- 适合存入代码片段、完整对话记录、原始日志等原文内容
- 健康探针：`raw_drawer_ok`

**🔍 Code Graph 代码图谱（吸收 code-review-graph AST 爆炸半径）**
- 新增 `POST /code/impact` — 分析文件改动波及范围（爆炸半径）
- 新增 `GET /code/graph` — 查看全项目代码依赖图
- AST 静态分析 + import 关系追踪
- 健康探针：`code_graph_ok`

**🛠️ MCP Server 重构（6工具 → 36工具）**
- 完全解耦：所有工具统一通过 HTTP 调用 api_server，消除 Qdrant 锁冲突
- 新增工具分组：Core CRUD / Facts / Code Graph / Session / Reflect / Core Memory / AutoDream / Raw Drawer / Knowledge Tree / Crystals / Conflict
- 工具接口与 REST API 保持一致

**🔗 IDE 集成（Cursor & Claude Code Hook）**
- 新增 `integrations/cursor-hook/` 目录
- `cursor-aidumem.mdc` — Cursor Rules 规则文件
- `aidumem-on-save.sh` — 文件保存时自动存入 Raw Drawer
- `claude-code-hook.py` — Claude Code 集成 CLI（store/search/impact/health）

### 代码质量
- 修复 `ducky/extended/routes.py` 裸 `except:` → `except Exception:`
- MCP Server 彻底移除直接 ducky 模块依赖，改为 HTTP 代理模式

### 竞品融合来源
- **MemPalace (58k⭐)**: Verbatim Storage → Raw Drawer
- **code-review-graph (29k⭐)**: AST blast radius → Code Graph
- **SimpleMem (3.7k⭐)**: EvolveMem → 检索自进化（Phase 3 规划）
- **Engram (5.8k⭐)**: 零依赖理念 → 部署收敛
- **OpenViking (27.7k⭐)**: 统一上下文 DB → Skills-Memory 融合（长远规划）

---



## v17.0.2 — Themis 忒弥斯 Docker构建构建顺序修复（2026-08-07）

> Docker 构建优化：调整 Dockerfile 中 COPY 源码与 pip install . 的顺序，解决容器构建时入口点 api_server 缺失导致的 ModuleNotFoundError。

### 变更

- **Dockerfile 构建顺序**: 调整 `COPY . /app` 优先于 `pip install .`，确保 setuptools 打包时 `api_server.py` 已入场。
- **版本号**: `17.0.1` → `17.0.2`（补丁版本，Themis 忒弥斯主线不变）

---

## v17.0.1 — Themis 忒弥斯 补丁（2026-08-07）

> 基座升级：mem0ai 2.0.15 → 2.0.17，获取最新 SDK 特性和安全修复

### 变更

- **mem0ai 基座**: `2.0.15` → `2.0.17`
  - 2.0.16: 新增 `reference_date` / `latest_only` / `keyword_search` 搜索选项
  - 2.0.16: Core 修复 metadata 剥离 (`user_id`/`agent_id`/`run_id`/`actor_id`)，防止身份范围被意外篡改
  - 2.0.16: 向量库修复（Upstash filter 校验、Supabase/Elasticsearch 分页边界）
  - 2.0.17: 新增 `agent_custom_instructions`，支持 agent 级别的自定义提取指令
- **依赖锁定**: `requirements.txt` + `pyproject.toml` 中 `mem0ai>=2.0.15` → `>=2.0.17`
- **版本号**: `17.0.0` → `17.0.1`（补丁版本，Themis 忒弥斯主线不变）

### 升级清单

1. `pip install --upgrade mem0ai==2.0.17`
2. `systemctl restart aidumem-api.service`
3. 验证 `/health` 返回 `"health_status":"ok"`

---

## v17.0.0 — Themis 忒弥斯（2026-08-06）

> 治理秩序纪元：将 Mímir 联邦记忆系统的三大治理理念融入 aiduMEM

### 核心新特性（借鉴 Mímir v9.1）

**🏛️ 变更事件账本 (fact_events)**
- 新建 `fact_events` 表，冲突消解动作自动留审计记录
- 记录 event_type / category / fact_key / new_value / affected_ids
- `schema_bootstrap.py` 开箱即建，新库无需手动迁移

**🔒 敏感级别分档 (sensitivity)**
- facts 表新增 `sensitivity` 列（internal / confidential / restricted）
- 默认 `internal`，现有数据无影响
- 为未来外发策略控制预留结构基础

**🛡️ SkillCrystallizer 治理铁律**
- 结晶候选项遵循"LLM 只能建议，人工 approve 才能落地"
- 新增 `approve_crystal()` 接口，status: candidate → approved
- 新增 `source_categories` / `sample_keys` 字段，过滤噪声分类（Experience/emotion/session 等）

### 代码质量修复

**ConflictResolver**
- 快速路径：先在内存做规则匹配，无命中不查 DB（避免冗余全表扫描）
- 规则集脱敏：MUTUAL_EXCLUSION_PATTERNS 改为通用占位符，运行时可注入业务规则
- 新增 `load_custom_exclusion_patterns()` 供 api_server 启动时配置

**TreeMemory**
- `fact_count` 改为精确匹配 `category`，去除 `tags LIKE %name%` 误匹配
- 新增 `get_ancestors()` 向上追溯接口
- 根节点改为通用模板，可自定义注入

**SkillCrystallizer**
- `procedure` 只记录操作键摘要，不再 GROUP_CONCAT 完整 Experience 内容
- 结晶阈值：分类下 ≥ 3 条有效 fact 才触发，减少无意义结晶
- 新增 `candidate_count` 字段追踪候选事实数量

---


## v16.0 — "Opus Octopod · opus八爪鱼"（2026-08-06）

**一句话**：借鉴 MemOS 三大优势，实现显式冲突消解、树状记忆图谱与碎片记忆向标准化技能自动结晶。

- **ConflictResolver 显式冲突消解器** (`ducky/conflict_resolver.py`)：Key-Value 覆盖 + 规则匹配（如域名迁移、名称变动），`valid_to` 降权失效
- **TreeMemory 树状记忆图谱** (`ducky/tree_memory.py`)：`memory_nodes` 表 + `node_path` 层级追溯与 Facts 节点挂载
- **SkillCrystallizer 技能自动结晶器** (`ducky/skill_crystallizer.py`)：后台 consolidator 自动感知高频重复事实并提炼为 Skill 候选项
- **专属 REST 端点**：`/conflict/resolve`、`/tree/nodes`、`/tree/node`、`/crystals`、`/crystals/detect`

---

## v0 — "初啼"（2026-06-13）

**一句话**：mem0 裸壳上线，为 AI Agent 提供基础记忆能力。

- 部署 mem0 + Qdrant + SQLite
- FastAPI 包装 5 个端点：`/add /search /recent /stats /delete`
- facts.db 建表：id / category / fact_key / fact_value / source / confidence
- 33 条初始事实（用户 × 9 + AI × 6 + 暗号 × 5 + 场景 × 6 + 其他 × 7）

---

## v1 — "无懈可击"（2026-06-14）

**一句话**：借鉴 memory-os 7 层 + OpenViking 4 件套，打造「5 大块升级免疫」系统。

- **Phase 1**：`requirements.txt` + `CUSTOMIZATIONS.md` + 5 端点 smoke test + pre/post-check.sh
- **Phase 2.1**：L0/L1/L2 三层加载（summary / overview / fact_value）
- **Phase 2.2**：目录递归检索 + trajectory 数组
- **Phase 3**：7/7 端到端测试 + 50 问句性能基线
- **Phase 3A**：trust_score、helpful/unhelpful、Bayesian 信任分
- L0 模式节省 55.3% token，search P50 = 3.5ms

---

## v2 — "混合召回"（2026-06-24）

**一句话**：FTS5 全文索引 + 加权混合召回，对标 Hindsight TEMPR。

- FTS5 建索引：`CREATE VIRTUAL TABLE facts_fts USING fts5(...)`
- 向量（向量嵌入）+ BM25 + 时效 + 可靠性 + 热度，5 维融合
- `/facts/search` 支持 keyword + category 联合查询

---

## v3 — "半衰期 + 矛盾检测"（2026-06-29）

**一句话**：信任衰减 + 矛盾发现，记忆质量自愈。

- Bayesian decay：trust_score 半衰期衰减（月 cron）
- Jaccard 去重（threshold 0.85，周日 cron）
- `/prune/contradiction` v1：矛盾词匹配 + 自动标记
- Social Closer Filter（auto_memory.py 过滤寒暄）
- `/facts/feedback`：helpful/unhelpful → trust 动态调整

---

## v4 — "Holographic 实体解析"（2026-07-10）

**一句话**：v4 — 实体链接 + 多实体推理，Holographic 植入。

- 实体提取器：分词 → 提取 → 消歧 → link → 存入 `entities` 表
- `/facts/entities`：按实体查询所有关联 facts
- `/facts/reason`：多实体联合推理（e.g. "用户 + AI"）
- `/facts/related`：Holographic 'related' 发现
- `/prune/contradiction-v2`：Holographic 语义矛盾检测
- **12 脉融合**：mem0 + memory-os + DIKW + Hindsight + TencentDB + Hermes Holographic + Honcho + RetainDB + ByteRover + Supermemory + Honcho Peer + RetainDB Preference

---

## v5/v6 — "15 脉 + 自动遗忘"（2026-07-10~12）

**一句话**：15 脉融合 + 后台自动遗忘/压缩，记忆自我管理。

- 15 脉：新增 RetainDB Delta / Supermemory / ByteRover 三脉
- 后台线程统一 `_BG_THREADS` 字典管理
- 自动遗忘：trust < 0.2 自动归档
- consolidation 后台线程
- `/scene` + `/scene/cluster`：场景聚类（对标 memory-os scenes）

---

## v7 — "Aion"（2026-07-12）

**一句话**：借鉴 Aion Memory 三层自主架构，4 大自主模块上线。

- **Layer 1 写入自检**：`/add` 自动去重 + 容量检测 + 自动合并
- **Recall Funnel**：`/search_trace` 端点，4 阶段搜索链路可观测
- **加权混合召回**：向量 + BM25 + 时效 + 可靠性 + 热度，5 维融合升级
- **Instinct→Skill 自动毕业**：`/graduate` 端点，同域 ≥3 条自动蒸馏
- 统一版本号：头注释 / logger / FastAPI title → `aiduMEM-v7`
- 旧 `_hybrid_search()` 委托给新 `ducky.hybrid_recall`
- 健康检查升级：`/health` 返回模块状态

---

## v8 — "Prometheus"（2026-07-12/13）★ 当前

**一句话**：五脉架构 + 大重构 — api_server 瘦身 39%，ducky/ 模块化，legacy 归档。

### 五脉架构
| 脉 | 模块 | 职责 |
|----|------|------|
| Ignition | `memory_ignition.py` | 记忆火花 — 写入时自动触发 |
| Workspace | `memory_workspace.py` | 工作空间 — 活跃记忆区 |
| Broadcast | `memory_broadcast.py` | 记忆广播 — 跨域传播 |
| J-lens | `memory_jlens.py` | J 透镜 — 记忆视角扭曲 |
| Persistence | `memory_persistence.py` | 持久化 — 长期稳定储存 |

### 大重构
- `api_server.py`：1613 → 988 行（-39%）
- `ducky/utils.py`：提取 7 个共享工具函数
- `ducky/legacy_routes.py`：迁移 §5-§10 SQLite 端点
- `legacy/archive/`：退役 19 个不再使用的脚本
- 13/13 ducky 模块独立导入通过
- 22/23 端点冒烟通过

### 修复 3 个原代码 SQL bug
- `scene/cluster`：scenes.db → facts.db（连错库）
- `/observe`：stale → is_stale（列名错误）
- `/facts/related`：e2.name 别名在子查询外引用

---

## v9 — "Tahoe-Gate"（2026-07-16）

**一句话**：引入相关性闸门与情绪半衰，零退化永久分轨。

- 相关性闸门 (Relevance Gate) 启发式联想匹配，节省 token
- 零退化分轨：identity/preference 设置 DECAY_MULTIPLIER=0.0
- 情绪加速半衰：emotion 设置 DECAY_MULTIPLIER=1.5
- FTS5 trigram 切词

---

## v9.1 — "Mnemosyne"（2026-07-21）

**一句话**：潮浪并忆 (Coalesce) 异步合并队列，三档按 profile 加速。

- 引入会话合并队列 (Coalesce)，async 短句缓冲合并写入
- tech/default/intimate 三档 profile 分离
- 优化 /add 写入速度

---

## v9.2 — "Lethe"（2026-07-26）

**一句话**：昨晚初步融入 EchoMind (声声) 基础组件。

- 引入 Ebbinghaus 指数遗忘初步公式与 Lane 轨道半衰期概念
- 数据库新增演化追踪支持

---

## v9.3 — "Aletheia"（2026-07-27）

**一句话**：阿勒忒亚真理版，安全高效完全植入与命名对齐。

- **品牌命名对齐**：统一为 **`aiduMEM`** 命名规范
- **Ebbinghaus 遗忘曲线**：整合指数遗忘曲线与 Lane 分轨，使衰退更符合人类心理学，且永久保留铁轨分轨
- **用户纠正感知**：检测到用户的纠错词（如“不对”“记错了”），相关性闸门秒级激活，强行检索以纠正事实
- **知识演化追踪 + 物理隔离**：自动检测 `replaces/enriches` 关系，中文特化共同名词检测，被取代记忆标记为 `superseded`，在检索中进行物理过滤
- **Memory Health Report**：新增 `/api/memory/health` 端点，对生命周期与演变链路进行全景健康诊断
- **底层重组**：彻底在 `ducky/utils.py` 补全连接工厂 `get_*_conn()`，解决之前潜在的导入 bug，确保自动遗忘线程绝对稳定

---

## v15.0 — "Iris"（2026-08-04）

**一句话**：伊里斯彩虹桥——接上 Hermes 官方记忆通道，并让所有「静默失效」全部出声。

### 🌈 官方通道（Native Provider Bridge）

- **新增 Hermes MemoryProvider 插件**（`integrations/hermes-plugin/aidumem/`）：
  `cp -r` 到 `~/.hermes/plugins/` + `hermes config set memory.provider aidumem` 即接入
- 拿到全套生命周期钩子，此前走 shell hook 一个都拿不到：
  - `prefetch` — turn 开头注入 CoreMemory 常驻块 + 本轮相关检索
  - `sync_turn` — 每轮对话后台归档，不阻塞对话
  - `on_pre_compress` — **压缩前把即将被丢掉的轮次先落进长期记忆**
  - `on_memory_write` — 镜像宿主内置 MEMORY.md / USER.md 写入
  - `on_session_end` — 触发服务端归档与反思
  - `get_tool_schemas` — `aidumem_search` / `aidumem_remember` / `aidumem_status` 三个工具
  - `backup_paths` — 数据目录纳入宿主备份流程
- 所有调用失败一律降级为「无记忆」，绝不影响宿主对话

### 🔊 静默失效清零

三类「不报错但一直没生效」的坑，本版全部堵上：

- **注入链断了不出声** → shell hook 的 payload 解析从只认顶层 `messages` 改为三层兼容
  （`extra.conversation_history` / 顶层 / 旧 `messages`）。宿主 payload 形状变过一次，
  旧脚本因此长期返回空却退出码 0，谁都发现不了
- **词表漏配不出声** → 相关性闸门（`memory_gate.py`）与实体抽取（`hot/legacy.py`）的
  关键词正则从 import 时固化改为**惰性编译 + 热更新**。systemd 漏写 `Environment=` 时，
  旧版会静默把涉及自定义人名/项目代号的查询判成 no_signal 直接零召回
- **启动缺配置不出声** → `AIDUMEM_ENTITY_KEYWORDS` 未设置时，启动日志与 `/health` 探针
  都明确告警

### 🔧 其他

- 新增 `integrations/aidumem-inject.sh` 通用 hook（零硬编码，端口/身份/阈值全走环境变量），
  替换并删除旧 `integrations/mem0-inject.sh`（仓库版本长期停留在 v9，与运行版本已分叉）
- 新增 `reset_gate_cache()` 可测试性钩子，暴露闸门热缓存（`_GATE_CACHE_TTL=15s`）
- 新增 `.env.example`（带注释的全量环境变量清单）与 `deploy/aidumem-api.service` systemd 模板
- `/health` 探针加实体词表状态字段，部署方一眼看到词表是否生效
- 新增 20 个单元测试：`test_inject_hook.py`（8 个，三种 payload 形状 + 边界）、
  `test_memory_gate_entities.py`（12 个，词表惰性加载 + 热更新 + 正则元字符 + 缓存隔离）
- 文档：中英 README 补「接入 Hermes Agent」章节与**服务无鉴权安全警告**，
  重写 `integrations/INTEGRATION_GUIDE.md` 覆盖两种接入方式与回滚

---

## v14.0.1 — "Aegis Patch 1"（2026-08-02）

**一句话**：基座升级——同步升级 upstream mem0ai 至 2.0.15 稳定版。

- **基座升级**：适配 `mem0ai` 2.0.15，接入原生 `delete_all` 循环 Drain 批量删除机制与最新模型索引支持
- **零中断兼容**：验证五维融合召回、Tahoe-Gate 闸门、Chronos 双时间轴无缝兼容，全项健康探针 🟢 通过
- **依赖同步**：`requirements.txt` 升级锁定为 `mem0ai>=2.0.15`

---

## v14.0 — "Aegis"（2026-08-01）

**一句话**：埃癸斯神盾——零硬编码，环境变量注入，克隆即跑。

> 神盾护住的不是代码，是代码背后的人。
> 仓库里只留能力，不留主人的痕迹。

- **仓库根自解析**：`ducky/utils.py` 新增 `BASE_DIR` / `DATA_DIR` / `LOG_DIR` 单一真源，由 `__file__` 逐级上溯得出；全仓不再有任何写死的宿主机绝对路径，克隆到任何目录都能跑
- **32 个 `AIDUMEM_*` 环境变量**：数据目录、日志目录、配置文件、默认 user/agent、API 基址、systemd 服务名、L0/L1 分级词表、实体/运维/日期关键词、宿主 state.db、上游网关采集参数——全部可注入，全部有安全默认值，一个不设也能启动
- **身份零残留**：`core_memory.py` 三大默认 block 改为「该写什么」的说明式占位；相关性闸门与实体抽取的人名/作品词表从代码里移除，改由 `AIDUMEM_ENTITY_KEYWORDS` 注入；`user_id` / `source` / `agent_id` 默认值统一为 `default`
- **宿主解耦**：`auto_memory.py` / `mem0_sync.py` 不再假定宿主 Agent 的路径，未配置 `AIDUMEM_HOST_STATE_DB` / `AIDUMEM_HOST_MEMORY_MD` 时静默跳过而非报错——aiduMEM 可独立于任何 Agent 框架单独部署
- **上游网关采集可选化**：`ducky/router_usage.py` 整体重写，SSH 目标 / 私钥路径 / 库路径 / 模型白名单全走环境变量；顺手把原先字符串拼接的 SQL 改为参数化占位符，消除注入面
- **配置模板化**：新增 `mem0_config_local.json.example`，密钥位一律 `YOUR_*_KEY` 占位；真实配置留在 gitignore 里
- **仓库瘦身**：清掉内部升级记录与一次性迁移脚本，删除根目录与 `scripts/` 完全重复的 `health_check.py`（同 md5），共 5 个文件出仓
- **验证**：56 文件改动（+676 / −1018），全量 py 编译通过、bash/json 语法通过、25 个联邦与突触单测全绿、API 服务实跑健康

---

## v13.0 — "Pantheon"（2026-07-31）

**一句话**：万神殿——多 Agent / 多 Profile 联邦记忆，MoE 门控架构。

> 万神殿里住着所有神，但每次只请出需要的那一位。
> 底层建成完整的联邦基础设施，日常只激活当前 Agent 的热通道。

- **联邦身份体系**：`facts` 表新增 `agent_id` / `profile` / `shared`，每条记忆都知道「这是谁的」；`agents` 表做注册表（注册 / 心跳 / 休眠 / 归属 profile）
- **分层衰减记忆**：三层差异化生命周期——`episodic` 事件 30 天、`semantic` 配置 180 天、`procedural` 铁律**永不衰减**；衰减只降权不删行，指数半衰永不归零
- **四级无缝降级检索**：L1 本 Agent 热通道 → L2 分层加权重排 → L3 同 profile 联邦 → L4 跨 profile 全局兜底；任何一级异常自动跳下一级，永不整链失败
- **MoE 门控路由**：默认走热通道（一次 SQL，5ms 级），仅在显式请求或查询含联邦意图关键词时才激活联邦通道；单 Agent 环境下永远不付联邦成本
- **写入自动去重**：Jaccard 相似度三态判定——≥0.85 合并（不新增行，标签取并集）、≥0.70 更新（同一事实新版本）、<0.70 新增；可用 `dedup=false` 关闭
- **按需 Rerank**：`rerank=true` 时才做词级语义与分层得分融合（0.6 语义 + 0.4 分层），默认不做以保住热通道手感
- **联邦感知广播**：游标制拉取其他 Agent 的新共享事实，不重不漏、只读聚合不产生副本；`/federation/awareness` 一眼看清联邦态势
- **10 个新端点**：全部 `/federation/*` 前缀，与既有 60+ 端点零冲突
- **向后完全兼容**：schema 迁移只 ADD COLUMN，历史 1118 条事实自动归属默认 Agent；不传 `agent_id` 的旧调用方行为与 v12 完全一致
- **25 个单元测试**：schema 幂等 / 分层衰减 / 去重三态 / 注册表 / 四级降级 / MoE 门控 / 广播游标，全部在临时库上跑，不碰生产数据

---

## 版本速查

| 版本 | 日期 | 代号 | 关键交付 |
|------|------|------|------|
| v0 | 06-13 | 初啼 | mem0 裸壳 + 33 条事实 |
| v1 | 06-14 | 无懈可击 | L0/L1/L2 + 升级免疫 + 测试体系 |
| v2 | 06-24 | 混合召回 | FTS5 + 5 维融合 |
| v3 | 06-29 | 半衰期 | decay + dedup + 矛盾检测 v1 |
| v4 | 07-10 | Holographic | 实体链接 + 多实体推理 + 12 脉 |
| v5/v6 | 07-10~12 | 15 脉 | 15 脉 + 自动遗忘 + 场景聚类 |
| v7 | 07-12 | Aion | 4 大自主模块 |
| v8 | 07-12/13 | Prometheus | 五脉架构 + 瘦身 39% ★ |
| v9 | 07-16 | Tahoe-Gate | 相关性闸门 + 情绪衰减 |
| v9.1 | 07-21 | Mnemosyne | 潮浪并忆 + 异步加速 |
| v9.2 | 07-26 | Lethe | 昨晚初步融入 EchoMind 基础依赖 |
| v9.3 | 07-27 | Aletheia | 阿勒忒亚真理版：四大功能完全植入 + aiduMEM 统一命名 |
| v11.1 | 07-29 | Hyperion | 光之泰坦：线程本地连接池 · 性能纪元 |
| v12.0 | 07-30 | Chronos | 时间泰坦：双时间轴 valid_from/valid_to · 失效降权不删除 |
| v13.0 | 07-31 | Pantheon | 万神殿：多 Agent 联邦 · MoE 门控 · 分层衰减 · 自动去重 |
| v14.0 | 08-01 | Aegis | 埃癸斯：零硬编码 · 32 个环境变量 · 隐私护盾 · 克隆即跑 |
| **v15.0** | **08-04** | **Iris** | **伊里斯：Hermes 官方 MemoryProvider 插件 · 静默失效清零 · 惰性热载词表 ★** |

---

## 技术脉络

```
mem0 裸壳 (v0)
  → L0/L1/L2 分层 (v1)
    → FTS5 + 混合检索 (v2)
      → 半衰期 + 去重 (v3)
        → Holographic 实体 (v4)
          → 15 脉 + 自动遗忘 (v5/v6)
            → 4 大自主模块 (v7)
              → 五脉模块化 (v8)
                → 相关性闸门 + 情绪衰减 (v9)
                  → 潮浪并忆 + 异步 (v9.1)
                    → Lethe (v9.2)
                      → Aletheia: aiduMEM 完全植入与命名对齐 (v9.3)
                        → Aletheia SE: 内存瘦身 + 向量磁盘化 (v9.3.1)
                          → Hyperion: 线程本地连接池 (v11.1)
                            → Chronos: 双时间轴有效期 (v12.0)
                              → Pantheon: 多 Agent 联邦 + MoE 门控 (v13.0)
                                → Aegis: 零硬编码 + 环境注入 + 可移植 (v14.0)
                                  → Iris: Hermes 官方 provider 通道 + 静默失效清零 (v15.0)
```

## 借鉴融合

| 来源 | 吸收了什么 |
|------|-----------|
| **mem0** | 向量存储（Qdrant + 向量嵌入） |
| **memory-os** | 7 层架构 · Facts 表 · Bayesian trust · 4 级权威 · FTS5 |
| **OpenViking** | L0/L1/L2 分层 · 目录递归 · viking:// 范式 |
| **Aion Memory** | Layer 1 自检 · Recall Funnel · Instinct→Skill 蒸馏 |
| **Hindsight TEMPR** | 5 维混合召回 · 时效权重 · search_trace |
| **DIKW** | 数据→信息→知识→智慧 金字塔 |
| **J-space** | 五脉架构（Ignition/Workspace/Broadcast/J-lens/Persistence） |
| **Hermes Holographic** | 实体链接 · 多实体推理 · 关联发现 |
| **Honcho** | Peer 记忆 · 跨用户关系 |
| **RetainDB** | Preference 存储 · Delta 增量 |
| **ByteRover** | 字节级记忆索引 |
| **Supermemory** | 热度权重 · 记忆排序 |
| **RL Feedback Loop** | trust_score 动态调整 · helpful/unhelpful |
| **TencentDB** | 大规模结构化事实管理 |
| **EchoMind** | Ebbinghaus指数遗忘曲线 · 知识演化(replaces/enriches) · 用户纠错信号感知 |
| **MoE (Mixture-of-Experts)** | 全量基建 + 稀疏激活的门控思想 → 热通道 / 联邦通道分流 |
| **多 Agent 联邦记忆范式** | Agent 注册表 · profile 隔离 · 游标广播 · 分层记忆生命周期 |


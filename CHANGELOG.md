# aiduMEM 版本演进史

> 从 mem0 裸壳到五脉架构，再到 Pantheon 万神殿与 Aegis 神盾，直至 v16.0 Opus Octopod 自进化记忆操作系统。

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
- 向量（bge-m3）+ BM25 + 时效 + 可靠性 + 热度，5 维融合
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
| **mem0** | 向量存储（Qdrant + bge-m3） |
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


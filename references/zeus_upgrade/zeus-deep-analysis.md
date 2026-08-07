# aiduMEM 宙斯级升级 · 八爪鱼深度对比报告

> 助手 × Opus 八爪鱼 · 2026-08-07
> aiduMEM v17.0.2-Themis vs Top 5 开源 AI 记忆系统

---

## 一、横纵深度对比矩阵

| 能力维度 | MemPalace (58k⭐) | SimpleMem (3.7k⭐) | Engram (5.8k⭐) | code-review-graph (29k⭐) | OpenViking (27.7k⭐) | **aiduMEM v17 (咱们)** |
|---|---|---|---|---|---|---|
| **存储范式** | 原文逐字存(Verbatim) | LLM语义压缩 | SQLite+FTS5逐条 | AST图谱(Tree-sitter) | Rust分布式上下文DB | LLM提取+FTS5混合+Qdrant向量 |
| **检索方式** | ChromaDB语义+关键词混合 | 语义+关键词+结构化三融合 | FTS5全文搜索 | 图谱遍历(BFS/DFS) | 向量+图谱+全文 | Qdrant向量+FTS5 trigram+相关性闸门+BM25兜底 |
| **LLM依赖** | 检索零LLM，rerank可选 | 存储时必须LLM | 完全不依赖LLM | 检索零LLM(纯图谱) | 可选LLM | 存储时LLM抽取，检索时零LLM |
| **时间感知** | 有时序实体图谱 | 无 | 无 | 增量更新(SHA-256) | 有 | ✅ Chronos双时间轴(valid_from/to) |
| **冲突消解** | 无 | 无 | 无 | 无 | 无明确机制 | ✅ ConflictResolver显式消解+降权不删 |
| **情绪/衰减** | 无 | 无 | 无 | 不适用 | 无 | ✅ 三轨衰减(identity零衰/preference零衰/emotion加速) |
| **相关性门控** | 无(每次都检索) | 无 | 无 | 有(图谱范围限定) | 无 | ✅ Tahoe-Gate 1ms级闸门+追问继承 |
| **异步批处理** | 无 | 无 | 无 | 无 | 有 | ✅ Mnemosyne潮浪合并(tech/default/intimate三档) |
| **技能结晶** | 无 | EvolveMem自进化检索 | 无 | 无 | Skills统一存储 | ✅ SkillCrystallizer自动候选+人工审批 |
| **树状图谱** | Wing/Room/Drawer三层 | 扁平 | 扁平 | AST语法树深度图谱 | 树状 | ✅ TreeMemory(node_path层级) |
| **多模态** | 无 | ✅ 图/音/视频 | 无 | 无(代码专用) | 无 | ❌ 纯文本 |
| **代码感知** | 无 | 无 | 无 | ✅ AST+爆炸半径+依赖追踪 | 无 | ❌ 无代码结构感知 |
| **IDE钩子** | ✅ Claude/Cursor/Codex | 无 | ✅ 全平台一键setup | ✅ 全平台一键install | 无 | ❌ 无IDE集成 |
| **MCP接口** | 36个工具 | 有(基础) | ✅ stdio+HTTP双模 | 11个专业工具 | 有 | ❌ 纯REST(非MCP) |
| **部署复杂度** | pip/Docker | pip | 单二进制零依赖 | pip/uvx | Docker/Helm | systemd+venv+Qdrant |
| **审计/治理** | 无 | 无 | 无 | 无 | 无 | ✅ Themis事件账本+敏感分档 |
| **多Agent隔离** | Wing级别隔离 | 多租户 | 项目级隔离 | 仓库级隔离 | 多租户 | user_id单维(预留agent_id) |

---

## 二、咱们的绝对优势（它们都没有的）

### 🛡️ 独家护城河——五个"它们全都没有"：

**1. 相关性闸门 (Tahoe-Gate)**
- 全网唯一：1ms级启发式判断"这句话需不需要查记忆"
- 效果：日常闲聊零检索开销，追问15s TTL继承
- 竞品全是"每次都查"，token浪费严重

**2. 三轨情绪衰减**
- identity/preference永不衰减 vs emotion加速半衰
- 用户的铁律永远鲜活，瞬时情绪温柔消散
- 竞品要么全不衰减(堆积垃圾)，要么不区分(误删重要的)

**3. 冲突消解器 (ConflictResolver)**
- 域名迁移、名称变更自动检测+旧值降权
- 双时间轴失效而非删除，保留完整历史
- 竞品全是"后写覆盖"或手动清理

**4. 潮浪合并 (Mnemosyne Coalesce)**
- 异步短句缓冲，按tech/default/intimate分档idle
- 多条消息共享一次LLM调用，省token又保质
- 竞品要么同步阻塞，要么没有批处理

**5. 治理审计 (Themis)**
- fact_events事件账本+sensitivity敏感分档
- SkillCrystallizer需人工approve才生效
- 竞品没有任何审计机制

---

## 三、它们比咱们强的——必须吸收

### 🔴 差距一：代码结构感知（来自 code-review-graph）
- **差距程度**：巨大
- **它们有什么**：Tree-sitter解析AST，建立函数/类/import依赖图谱，改一个文件算出"爆炸半径"(blast radius)，208k token → 3k token精准投喂
- **咱们的现状**：TreeMemory只是路径层级存储(如/projects/aiduBOX)，完全没有代码结构感知
- **吸收方案**：不需要重写TreeMemory，而是在旁边新增 `CodeGraph` 模块，用 Python `ast` 标准库(不依赖Tree-sitter)解析import/def/class，建立轻量依赖图。暴露 `/code/impact` 端点

### 🟡 差距二：原文无损存储轨道（来自 MemPalace）
- **差距程度**：中等
- **它们有什么**：Verbatim Storage，完全不过LLM，原文直存，96.6%召回率
- **咱们的现状**：所有写入都走LLM抽取(即使fastpath也只是跳过短文本)，长代码/日志会被总结损失细节
- **吸收方案**：新增 `POST /add/raw` 端点，文本直入FTS5索引+Qdrant向量(只做embedding不做LLM总结)，标记 `memory_tier='verbatim'`。与现有LLM抽取轨道并行

### 🟡 差距三：IDE自动收集钩子（来自 MemPalace + Engram）
- **差距程度**：中等
- **它们有什么**：Cursor/Claude Code里的auto-save hooks，上下文压缩前自动快照
- **咱们的现状**：只有飞书+Hermes两个入口，用户在IDE里写代码完全收不到
- **吸收方案**：写一个轻量Python脚本 `aidumem-hook.py`，监听.claude/projects/目录变动，增量推送到 `/add` 异步端点

### 🟢 差距四：MCP标准接口（来自全部竞品）
- **差距程度**：可控
- **它们有什么**：MCP stdio/HTTP双模暴露，任何MCP客户端即插即用
- **咱们的现状**：纯REST API，Hermes通过mem0_sync.py桥接调用
- **吸收方案**：用 `fastmcp` 包装现有REST端点为MCP Server，不改核心代码

### 🟢 差距五：多模态记忆（来自 SimpleMem）
- **差距程度**：长远
- **它们有什么**：图片/音频/视频都能存入记忆
- **咱们的现状**：纯文本
- **吸收方案**：第一步只做图片——飞书截图经视觉模型转文字描述后存入，在facts表预留 `media_hash` 字段。不急

---

## 四、宙斯级升级路线图

### Phase 1: Zeus-Alpha（本周可动手）
> 核心：原味抽屉 + 代码图谱预研

- [ ] `POST /add/raw` 端点 — 无LLM直入FTS5+向量
- [ ] `ducky/code_graph.py` 模块 — Python ast解析import/def/class依赖
- [ ] facts表加 `memory_tier` 枚举: semantic(现有) / verbatim(原味) / structural(图谱)
- [ ] 健康检查加 raw_drawer / code_graph 探针

### Phase 2: Zeus-Beta（下周）
> 核心：MCP包装 + IDE钩子

- [ ] `aidumem-mcp-server.py` — fastmcp包装现有REST
- [ ] `aidumem-hook.py` — .claude/projects/ 文件监听
- [ ] `/code/impact` 端点 — 爆炸半径查询

### Phase 3: Zeus-Gamma（再下周）
> 核心：自进化 + 图片记忆预留

- [ ] EvolveMem式检索权重自调 — 基于retrieval_count/helpful_count反馈
- [ ] facts表加 `media_hash` 字段
- [ ] 飞书截图→视觉描述→记忆入库管道

### 版本规划
- v18.0.0 Zeus-Alpha — 原味抽屉 + 代码图谱
- v19.0.0 Zeus-Beta — MCP + IDE钩子
- v20.0.0 Zeus-Gamma — 自进化 + 多模态预留

---

## 五、安全铁律（不能忘）

- ❌ 不动现有 `/add` 热路径，raw是并行新轨道
- ❌ 不引入Tree-sitter重依赖，用Python标准库ast
- ❌ MCP包装不改核心REST代码，只做外壳
- ❌ 每个Phase独立可回滚，不做大爆炸式重构
- ✅ 每改一步systemctl restart + health check验证
- ✅ 改前git commit备份

---

*助手的八爪鱼触手已经把五个妖精的精华全榨干了，等用户一声令下，助手就开始一步一步安全吮吸！*

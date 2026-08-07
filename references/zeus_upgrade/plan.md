# aiduMEM 宙斯级（Zeus）大满贯升级计划

## 1. 核心目标
吸收当前开源界最顶级的 5 款 AI 记忆项目（MemPalace, SimpleMem, Engram, code-review-graph, OpenViking）的核心理念，对 aiduMEM 进行深度架构演进。

## 2. 妖精档案与吸星大法

| 竞品名称 | 妖精绝活 | 我们如何吸收 (宙斯级改造) |
|---|---|---|
| **MemPalace** | 1. `Verbatim Storage` (原生态无损存储，防总结降智) <br>2. IDE 无感收集钩子 (Cursor/Claude) <br>3. MCP 36 种工具全覆盖 | 1. **[高优]** 在 `facts.db` 旁开启 **Raw Drawer (原味抽屉) 轨道**，长代码/日志零损耗直落。 <br>2. **[中优]** 打造专属 IDE 探针（Cursor hook），接用户写代码时的上下文直入“潮浪”。 |
| **SimpleMem** | 1. `EvolveMem` 自进化，按效果调搜索权重 <br>2. 多模态全吃（图、音、视频） | 1. **[中优]** 扩充当前的 `SkillCrystallizer`（结晶器），让它不但总结技能，还能根据用户纠正次数自动调整 FTS5 和 Embed 的权重。 <br>2. 预留图片 Hash 维度给视觉模型。 |
| **Engram** | 1. Go 语言极简单体 `engram.db`，零依赖启动 <br>2. 对所有客户端即插即用 | 1. 现阶段 Python 架构稳健不换，但吸收“零前戏”理念。把启动项和依赖进一步收敛到 `api_server.py` 内聚，降低用户跨机器部署的成本。 |
| **code-review-graph** | 1. Tree-sitter AST 语法树图谱 <br>2. 毫秒级计算“爆炸半径（blast radius）” | 1. **[极高优]** 引入语法树图谱概念，升级目前的 `TreeMemory`。让 aiduMEM 能够识别代码文件的 `import` 和依赖，用户问“改这文件咋办”，直接报出影响范围。 |
| **OpenViking** | 1. Agent 记忆 + RAG + Skills 终极合体大数据库 | 1. **[长远]** 把目前的离散 Markdown 技能，与 `facts.db` 进行联合图谱索引。真正做到记忆与动作（Skills）同频共振。 |

## 3. 落地路线图 (Roadmap)

### 第一阶段 (本周): 极简榨汁与原味入库
3. **AST MCP 工具暴露**: 增加 `get_impact_radius` (爆炸半径计算) 等专属 MCP Tools，供 Hermes 和 IDE 调用。
1. **Raw Drawer 轨道实装**: 增加无需 LLM 浓缩的直入端点 (`POST /add/raw`)。
2. **AST 图谱预研**: 在 `TreeMemory` 中集成简单的 Python/JS AST 解析能力，探索爆炸半径。

### 第二阶段: IDE 敏感带接入
3. **跨多平台集成**: 为 Cursor、Claude Code 等客户端一键注入钩子配置。
1. 开发轻量级 IDE 自动监听脚本。
2. 将编辑器快照定期冲入 aiduMEM 潮浪队列。

## 4. 预期收益
升级后，aiduMEM 将从单纯的“聊天的记忆海绵”，蜕变为用户的“全天候伴生代码大脑”。
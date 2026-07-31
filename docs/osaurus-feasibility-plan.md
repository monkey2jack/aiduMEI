# aiduMEM × Osaurus 可行性对标方案

> 调研日期：2026-07-15
> 目标项目：[Osaurus](https://github.com/osaurus-ai/osaurus) — 6.8k⭐，423 次 release，纯 Swift 原生 macOS AI 底座
> 核心洞察：Own your AI. The harness is what compounds. Inference is all you need.

---

## 一、Osaurus 是什么

一个 **macOS 原生 AI 底座**，不绑定任何模型，专注做模型外层的"不可替代层"——上下文、记忆、工具、身份。

```
┌─────────────────────────────────────────┐
│              模型（可替换）                │
│  MLX 本地 · Apple Foundation · 云端 API   │
├─────────────────────────────────────────┤
│            底座（Osaurus 的价值）          │
│  记忆 · 技能 · 身份 · 沙箱 · 自动化 · MCP  │
└─────────────────────────────────────────┘
```

**市场验证信号**：Homebrew 安装、活跃社区、49 个原生插件、20+ 篇深层文档、完整插件商店

---

## 二、Osaurus Memory vs aiduMEM 对比

| 维度 | aiduMEM (mem0) | Osaurus Memory |
|------|-------------|----------------|
| **分层** | workspace → ignition → instinct → persistence | Identity → Pinned Facts → Episodes → Transcript |
| **写入时机** | 实时 / 会话后 | **延迟蒸馏**：60s 去抖 + 80 字新颖性门，一整场只调一次 LLM |
| **注入预算** | 取决于上下文窗口 | **≤800 tokens**，多数轮次注入 0 |
| **检索方式** | 向量 + 关键词混合 | **相关性闸门**(启发式 + 可选 LLM) 先判断要不要，再按 scope 取值 |
| **衰减/清理** | mem0 自带 | **显著性衰减** `exp(-Δdays/30)`，低于 0.2 + 闲置 30 天自动踢出 |
| **后台维护** | - | **24h 合并器**：衰减、合并、踢出，不碰请求路径 |
| **配置项** | 较多 | **v2 精简到 8 项**，v1 的 18 个 knobs 全部砍掉 |

### 🎯 aiduMEM 可学的三个精华

**① 写路径——延迟蒸馏**
当前 aiduMEM 写入靠 mem0 的 `add()` 或 cron 触发。Osaurus 的做法：会话中只做 `bufferTurn`（一条 INSERT），会话结束后才用一次 LLM 蒸馏整场对话，产出 episode + entities + pinned candidates + identity delta。不抢主请求的 token 预算，不拖慢响应。

**② 读路径——相关性闸门**
当前 aiduMEM 检索后直接拼到 prompt。Osaurus 先过闸门：代词/关键词/实体命中 → 取对应 scope → 拼 ≤800 token 上下文块 → 否则直接跳过，零注入。把"会不会有用"的判断前置，避免无关记忆污染上下文。

**③ 显著性衰减 + 自动踢出**
当前 aiduMEM 欠缺自动清理机制。Osaurus 每 24h 跑一次合并器：`salience *= exp(-Δdays/30)`，低于阈值 + 闲置超时 → 自动踢出。记忆越用越亮，不用自然褪色。

---

## 三、Osaurus Plugin 系统 vs aiduMEM 插件

| 维度 | aiduMEM | Osaurus Plugin |
|------|-------|----------------|
| **ABI** | Python import | 稳定的 C ABI v6（向后兼容至 v1），25 个 slot 冻结布局 |
| **宿主 API** | MCP 工具调用 | 配置(Keychain) · SQLite 存储 · 推理(同步+流) · 分发(后台) · HTTP(SSRF) · 文件IO · embed · agent上下文 |
| **热重载** | 重启服务 | `osaurus tools dev` 文件保存即重载，无需重启 |
| **生命周期** | Python 模块 | `init → manifest → invoke/handle_route → destroy` |
| **分发** | 源码/Git | `.dylib` + Minisign 签名 + 插件商店 |
| **权限模型** | 无 | 安装时一次性同意：`network` / `filesystem` · `auto/ask/deny` 三层 |

### 🎯 aiduMEM 可学的两个精华

**④ 插件权限声明**
每个 tool 在 manifest 里声明 `requirements: ["network", "filesystem"]` 和 `permission_policy: "auto"|"ask"|"deny"`。用户安装时看到并一次性同意。aiduMEM 当前无此机制。

**⑤ 工具返回信封契约**
Osaurus 定义了统一的 `ToolEnvelope`：成功时 `{result, warnings}`，失败时 `{kind, message, retryable}` 含 8 种标准错误类型。aiduMEM 的 MCP tools 返回格式不统一。

---

## 四、Osaurus Skills & Methods（aiduMEM 已有类似物）

| Osaurus | aiduMEM 对等物 |
|----------|-------------|
| Skills（SKILL.md + references + assets） | Hermes skill 目录（已有 49 个） |
| `capabilities_discover / capabilities_load` | Hermes 的 skill_view / skill_manage |
| Methods（learned workflows） | aiduMEM 的 ignition/instinct_graduation |
| Claude Plugin import | ❌ 无 |

### 🎯 aiduMEM 可学的

**⑥ Methods = 自动学到的技能**
Osaurus 的 Methods 是 agent 自己从完成任务中提炼出的可复用工作流，存到 SQLite，下次相似任务自动匹配。aiduMEM 的 `instinct_graduation` 有相似理念但未体系化——可以借鉴 Methods 的存储/检索/匹配的设计。

---

## 五、aiduMEM 独特优势（Osaurus 没有的）

| 优势 | 说明 |
|------|------|
| **MCP Server 原生** | aiduMEM 本身就是一个 MCP server，直接对接到任何 MCP 客户端 |
| **Hybrid Recall** | 向量 + 关键词双路召回，Osaurus 的 relevance gate 更粗粒度 |
| **Ignition / Instinct Graduation** | aiduMEM 独有的"灵犀初燃→本能沉淀"进化体系 |
| **JLens** | 记忆的"自我审视"视角 |
| **跨平台** | Python 生态，不限于 macOS |
| **飞书深度集成** | 敖氏卡片、流式面板、cron 调度 |

---

## 六、可行性路线图

### Phase 1：记忆瘦身（1-2 周）

```
目标：借鉴 Osaurus Memory 的延迟蒸馏 + 显著性衰减 + 相关性闸门
```

| ID | 任务 | 优先级 | 工作量 |
|----|------|--------|--------|
| M1 | **写路径延迟化**：会话结束时触发摘要（而非每次 add），用 debounce + 新颖性门 | 🔴 高 | 3-5d |
| M2 | **显著性衰减**：给每条记忆加 salience 字段，`exp(-Δdays/30)` 衰减，自动踢出 | 🔴 高 | 2-3d |
| M3 | **相关性闸门**：检索前先判断是否相关（关键词/代词/话题匹配），不相关则零注入 | 🟡 中 | 3-4d |
| M4 | **24h 合并器**：后台 cron 做衰减+合并+踢出，不阻塞请求 | 🟡 中 | 2-3d |

### Phase 2：插件健康化（2-3 周）

```
目标：借鉴 Osaurus Plugin 的权限声明 + 返回信封 + 热加载
```

| ID | 任务 | 优先级 | 工作量 |
|----|------|--------|--------|
| P1 | **MCP Tools 统一返回信封**：`{result/warnings}` 成功，`{kind/message/retryable}` 失败 | 🟡 中 | 2-3d |
| P2 | **插件权限声明**：manifest 加 `requirements` + `permission_policy` | 🟢 低 | 2-3d |
| P3 | **热重载开发模式**：文件变更自动重载插件 | 🟢 低 | 2-3d |

### Phase 3：方法进化（3-4 周）

```
目标：借鉴 Osaurus Methods，让 aiduMEM 的 instinct_graduation 体系化
```

| ID | 任务 | 优先级 | 工作量 |
|----|------|--------|--------|
| W1 | **Methods 存储层**：SQLite 存储 agent 自学习的工作流模板 | 🟡 中 | 3-4d |
| W2 | **Methods 匹配检索**：基于任务相似度的自动匹配（BM25 + 向量） | 🟡 中 | 3-4d |
| W3 | **Methods 进化闭环**：完成→提炼→存储→下次匹配→优化 | 🟢 低 | 4-5d |

---

## 七、优先级矩阵

```
              高价值
                │
    M1 M2       │       M3 W1 W2
    (立即做)    │       (第二梯度)
                │
   ─────────────┼──────────────
                │
    P2 P3       │       W3 P1
    (低优先级)  │       (锦上添花)
                │
              低价值
  低成本 ─────────────────── 高成本
```

**推荐立即启动**：M1（延迟蒸馏）+ M2（显著性衰减）— 这两个改动小、价值高、不破坏现有架构。

---

## 八、Osaurus 项目指标（参考）

| 指标 | 数值 |
|------|------|
| Stars | 7.1k |
| 开源协议 | MIT |
| 语言 | Swift 69% + C 29% |
| 最低系统 | macOS 15.5 + Apple Silicon |
| Release 频率 | 高频迭代（0.21.7 → 已 423 次） |
| 插件数 | 20+ 原生 + 社区注册表 |
| 文档 | 20+ 篇深度文档 |
| 下载 | Homebrew + DMG |

---

## 九、结论

> Osaurus 和 aiduMEM 不在同一条赛道上——Osaurus 是 macOS 原生 AI 底座，aiduMEM 是跨平台的记忆引擎 + MCP 服务器。但 Osaurus 的 Memory 系统设计——延迟蒸馏、显著性衰减、相关性闸门、零阻塞维护——是 aiduMEM 可以直接吸收的精髓，改动小收益大。

**一句话**：Osaurus 教会我们"记忆不是越多越好，是越精准越好"——延迟蒸馏节约 LLM 调用、显著性衰减自动瘦身、相关性闸门避免噪音污染。aiduMEM 的 ignition/instinct 体系已经很超前，再加上这三个机制，记忆会从"能记住"进化到"该记住时才记住"。

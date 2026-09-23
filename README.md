<p align="center">
  <img src="assets/aidumei-banner.webp" alt="aiduMEI ⚕ 爱嘟优忆思 — Memory + Engine + Insight" width="100%">
</p>

<!-- distribution-policy: github-source-only -->

# aiduMEI ⚕ 爱嘟优忆思——智能体通用智慧引擎

> 让你的 AI Agent **真正记住你**：混合检索 + 认知治理 + 可视化控制台 + 双引擎自动挡，**单机自托管**，MIT。
> 宿主（Hermes / Claude Code / Cursor / 任何 MCP 客户端）管短期对话，aiduMEI 管长期记忆。

> **当前公开版本 f0.1。**
>
> **关于 `f`**：这是一个新的纪元，不是旧版本号的续写。`f` 取 **future / fantasy / forever** ——
> 我们想做的不是一个更大的缓存，而是一份能陪人走很久的记忆。版本号形态 `f<主>.<次>`，
> 常规迭代升末位，颠覆性改造才升首位。此前的版本历史全部归档在 [CHANGELOG](CHANGELOG.md)，
> README 只回答一件事：**它现在是什么。**
>
> **状态标签**：功能快速迭代期 · 外部校准 1/4（✅ 基准试跑已做并公开错题本 · ⬜ 依赖 · ⬜ 第三方复现 · ⬜ 独立审查）。
> 按 tag 检出 ≠ 可发布版本，以 `pyproject.toml` 为准。

---

## 优忆思：MEI 不止是「美」

**MEI = Memory + Engine + Insight** —— 记忆、引擎、洞察。中文叫**优忆思**，三个字各自兑现一件事，
**每件都对应仓库里真实在跑的代码**，不是修辞：

| | 一句话 | 它在代码里是什么 |
|---|---|---|
| **优** · Engine | **优化配置，双引擎全自动** | 双引擎自动挡（云端断供就地换本地备胎、恢复自动升挡）· 一行 Prompt 全自动部署 · 读/写/萃取三条钩子自己触发，接上之后你不用再管记忆 |
| **忆** · Memory | **记忆底座与记忆逻辑** | 三轨遗忘曲线 · 双时间轴（记忆**过期**而非删除）· 事件时间与入库时间分开存 · 向量 + 中文 BM25/trigram + cross-encoder 真重排（不是加权融合）· 六型分类 |
| **思** · Insight | **借大模型的思考力，省你的上下文** | 相关性闸门先拦掉闲聊（闲聊不检索）· 重排收窄再进上下文 · 会话萃取把一整程压成一条 · 反思与自进化（`reflect.py` / `evolve_mem.py`）；同时认知治理兜底——AI 只有「提出候选认知」的权限，**没有直接创造事实的权限** |

中间那行的后半句是这一代的主题。以前系统能答「你说过什么」，答不好「那是什么时候」——
因为写入时把**事情发生的时间**悄悄换成了**记录存进来的时间**，不报错、不告警，
检索照常返回、健康检查照常全绿。这一代把它修到了写入、检索、注入三处（见 [CHANGELOG](CHANGELOG.md)）。

## 三样市面独一份

> ⚠️ **证据状态**：以下三项为**功能组合独一份**（市面无同构实现），
> 「独一份」指功能组合，不是实测排名。分数请看下一节，我们连错题本一起交。

| 杀手锏 | 一句话 |
|---|---|
| 🚗 **双引擎自动挡** | 云端断供自动降本地备胎、恢复自动升挡、欠账自动重放——单查询内就地换腿，挡位永远如实可见 |
| 📊 **可视化控制台** | 零构建 Web 控制台（`/ui`）：记忆可见、可调、可追溯；可切换真实记忆域、按当前域导出 Markdown——「记忆是黑盒」这个行业痛点，我们正面回答 |
| 🧠 **认知治理** | 每条记忆**有出身**（你亲口/AI 推断/外部引用/不明，写入零成本打标）、**有户口**（溯源三件套：谁、哪次会话、第几轮）、**可导出**（一键 Markdown 记忆档案） |

## 📊 跑分：我们跑了，连错题本一起交

**2026-09-22 完成首次 LoCoMo 基准试跑。** 下面这张表里没有一个数字是推导的。

> ⚠️ **先说清口径，否则这张表会骗人。** 记忆系统跑分有**两把不可混用的尺子**：
> **F1**（逐词重叠，措辞不同即扣分，严）与 **LLM-Judge**（判语义等价，宽）。
> 业内挂在宣传里的「LoCoMo 90%+」几乎都是 Judge 口径且经方法迭代。
> **下表统一用 F1**，业内数字取自 Mem0 论文（[arXiv:2504.19413](https://arxiv.org/abs/2504.19413) Table 1，各系统作者自测）。

| 维度（F1 口径） | LangMem | Zep | OpenAI 全上下文 | Mem0 | **aiduMEI 试跑** | 我方位次 |
|---|---|---|---|---|---|---|
| 单跳直接召回 | 35.51 | 35.74 | 34.30 | **38.72** | **37.37** | 🥈 第 2 |
| 多跳推理 | 26.04 | 19.37 | 20.09 | **28.64** | 23.19 | 中游（超 Zep / OpenAI） |
| 时序推理 | 30.75 | **42.00** | 14.04 | **48.93** | 25.79 | ❌ 明确短板 |
| 开放域知识 | 40.91 | 49.56 | 39.31 | 47.65 | 9.87 | ⚠️ 仅 13 题，**无统计意义** |
| 对抗拒答（防幻觉） | — | — | — | — | **81.69** | ⭐ 业内四类对比通常不含此列 |

**试跑条件**（复现锚点）：LoCoMo 官方数据集 `3eb6f2c` 的**前 2 个完整样本**（conv-26 / conv-30）· 788 轮灌库 + 304 题 ·
嵌入 BAAI/bge-m3 · 答题 Claude-Sonnet-4.6 · 混合检索 top_k=5 · **零修正基线** · 总分 F1 42.14% / Judge 52.96%。

**这张表怎么读**：

- ✅ **可以讲的两条**：**单跳直接事实召回进第一梯队**（业内第二，反超 Zep / OpenAI 全上下文 / LangMem）；
  **防幻觉断层领先**（对抗拒答 F1 81.69）——面对「对话里根本没提过」的陷阱题，门控哲学让它极少被诱导编造。
- ❌ **必须承认的**：**时序推理是硬伤**（25.79 vs Zep 42 / Mem0 48.93）；开放域那 13 题**不构成结论**。
- ⚠️ **不能讲的**：这是**试跑**，不是正式成绩。2/10 样本、裁判用的是 Sonnet 而非业内口径的 GPT-4o。
  **我们没有、也不会在这个基础上宣称 SOTA。**

### 本版预期（目标，**尚未复跑验证**）

上表那条最扎眼的短板——时序推理——**本版已经动过手了**：根因定位在「写入时把事件时间换成了入库时间」，并在写入、检索、注入三处一起修好（详见 [CHANGELOG](CHANGELOG.md)）。

但**本版没有重跑评测**，所以下面「目标」一列一个都还不是成绩：

| 维度 | 试跑实测 | 目标 | 本版做了什么 | 状态 |
|---|---|---|---|---|
| **时序推理** | 25.79 | **≥ 40**（对标 Zep） | **本版主攻**：事件时间写对 · `/search` 透传 · 注入渲染带上日期 | 🔧 **已修，待复跑验证** |
| 单跳 | 37.37 | 守住 ≥ 37 | 未动检索主路径，不应退步 | 🛡️ 守 |
| 对抗拒答 | 81.69 | 守住 ≥ 80 | 门控哲学未变 | 🛡️ 守 |
| 多跳推理 | 23.19 | ≥ 28（对标 Mem0） | 本版未动 | ⬜ 留待后续（迭代检索 / 查询扩展） |
| 开放域 | 9.87 | 全量复跑取真值 | 本版未动 | ⬜ 留待后续（须先扩样本） |

> **为什么「已修」还不敢报成绩**：改对了代码不等于分数会涨。这条得等全量复跑用数字说话——
> 在那之前，上表右边写的是**我们打算兑现什么**，不是我们已经兑现了什么。

> **为什么先交一份不好看的成绩**：第一次跑分的价值是暴露问题，不是拿分数。
> 这次试跑直接炸出了一个藏了很久的根因（事件时间被静默换成入库时间），
> 那比一个漂亮数字有用得多。正式打榜须全量 10 样本 + GPT-4o 裁判复跑，
> 届时**连失败项一起公布**。评测协议已冻结在 `benchmarks/`（数据集、模型、judge、prompt、seed、文件哈希全部锁定留证）。

## 一键部署：让 Agent 干活，你看着

把这句话发给你的 AI Agent：

> 你现在是部署工程师。请按 <https://github.com/monkey2jack/aiduMEI> 的 `prompts/install.txt` 全文（14 行正典）在我这台机器上部署 aiduMEI，每步自己验证，不许假装成功。

正典会带它走完：环境检查 → 装依赖 → 选挡位 → 配 Key（向你索要，不编造）→ 起服务 → **真实写入/召回验证**（不是只看 `/health`）→ 接入宿主 → 装定时任务与备份 → 给你出报告。

> ⚠️ **接宿主时必须接两条线，不是一条。** 「读线」是每轮对话**之前**注入记忆，漏了你立刻就会发现；「写线」是每轮对话**之后**把内容记下来，**漏了你几周都发现不了**——检索照样有结果、`/health` 照样全绿，因为旧记忆确实健康，而你说的每句新话都在被丢掉。
>
> 这是我们自己在生产上吃过的亏（读线挂了一个月、写线从没挂过、所有探针全绿）。所以现在：`/health` 有 `ingest_liveness_ok` 探针盯着「在读却不在写」，并且真实用过几轮后请跑一次
>
> ```bash
> python3 scripts/check_ingest_wiring.py --token "$AIDUMEM_API_TOKEN"   # 退出码 0 才算接线成功
> ```
>
> **三条线各有现成脚本，拷过去注册上即可**（别自己写）：
>
> | | 脚本 | 挂点 | 自动做什么 |
> |---|---|---|---|
> | 读线 | `integrations/aidumem-inject.sh` | Hermes `pre_llm_call` | 每轮**之前**自动把相关记忆喂给模型 |
> | 写线 | `integrations/aidumem-ingest.sh` | Hermes `post_llm_call` | 每轮**之后**自动把这一轮存回去 |
> | 写线 | `integrations/cursor-hook/claude-code-stop-hook.py` | Claude Code `Stop` | 同上 |
> | 萃取线 | `integrations/aidumem-distill.sh` | Hermes `on_session_end` | 会话结束时自动提炼「这一程最值得记住的事」，单独存一条 |
>
> **「自动」是指这三条线接上之后，你不需要再对记忆做任何事**——不用手动保存、
> 不用提醒模型去记、不用定期整理。三条钩子分别在「说话前」「说话后」「聊完」
> 三个时机自己触发。你唯一要做的是把它们挂对位置，然后用下面那条命令验一次。
>
> 萃取线解决的是另一类遗忘：每轮写入存的是**事实**，存不下「这一程是怎么回事」——
> 随口说的一句话、一起解决的一个难题、某个决定的瞬间，会散成十几条事实再也浮不上来。
> 它走独立的慢衰减泳道（`distill`，比普通记忆留得久），情感权重取自本仓既有的
> 情绪词表，不是新造的分数。
>
> 三个脚本都带 `--selftest`（写线会真写一条再回读）。但**自检通过只证明脚本能跑，不证明宿主在调它**——那次事故里脚本一直是好的，没被挂上而已。所以上面那条 `check_ingest_wiring.py` 才是唯一的验收判据。完整挂法与 yaml 写法见 [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)。

### ⚠️ 给升级者的重要提示：改完代码必须重新部署钩子

**钩子是拷贝不是软链。** `git pull` 更新了仓库里的 `integrations/*.sh`，宿主执行的**仍然是旧文件**——不报错、不告警、日志干净、`/health` 全绿。读线会悄悄退回旧行为，而你以为已经升级了。

还有更阴的一层：**宿主实际调用的文件名可能和仓库不一样**。早期安装可能留下了别名。按文件名去核对，会验到一个宿主根本不执行的文件，然后得出「已部署」的错误结论。

**所以升级后（以及任何时候你想确认「宿主跑的是不是这一版」）**：

```bash
python3 scripts/check_hook_deployment.py     # 退出码 0 才算部署到位
```

它**不认文件名，只认 `~/.hermes/config.yaml`**：宿主声明调哪个路径，就去比哪个路径的 md5；发现漂移会直接把修复命令打给你。没挂钩子时它报「没测到」而不是「通过」。

这项检查已并入 `scripts/health_check.py`，**定时巡检会自动带上，你不需要另外配一条 cron**。

> 一句话记住：**验证要打在宿主真正调用的那个文件上——验仓库文件等于没验。**
> 这是我们被用户审计当场抓出来的（修复写好了、测试绿了、报告都发了，就是没送到宿主手上）。

**不用 Agent？手动五行：**

```bash
git clone https://github.com/monkey2jack/aiduMEI.git && cd aiduMEI
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp mem0_config_local.json.example mem0_config_local.json && cp .env.example .env   # 编辑这两个文件填 Key
python api_server.py                                                              # → http://127.0.0.1:8767
python scripts/e2e_smoke.py --json                                                # 真实验证，PASS 才算数
```

**三档引擎，按你的机器选**（`AIDUMEI_ENGINE_MODE=auto` 为默认推荐；`cloud` / `local` 同理）：

| 挡位 | 需要什么 | 效果 |
|---|---|---|
| `cloud` | 云端 LLM + Embedding Key | 最轻；断供时召回诚实降级 |
| `auto`（默认推荐） | Key + `pip install .[local-embed]` | 云优先、断供自动换本地、恢复自动换回 |
| `local` | 本地模型，零 Key | 零 token、零出站调用 |

容器化部署见 [docs/DEPLOY_DOCKHOLD.md](docs/DEPLOY_DOCKHOLD.md)；Agent 侧的完整作业说明（验收探针、备份、维护）在 [AGENTS.md](AGENTS.md)。

## 📦 负荷与消耗——两种体量，实测全摆在这里

> 这套东西部署起来重不重？**取决于你选哪个挡位。**（2 核 3.5G 云主机 · 2026-08-27 实测）

| 维度 | ☁️ 云端档（`cloud`） | ⚙️ 自动挡（`auto`，默认） | 🔋 本地档（`local`） |
|------|---------------------|--------------------------|---------------------|
| **运行内存** | **约 280 MB** | **约 430 MB** | 约 430 MB |
| **依赖磁盘** | 约 275 MB | 约 353 MB + 模型 91 MB | 同自动挡 |
| **断供时** | 无备胎，如实判 `degraded` | **自动降挡续跑** | 不依赖外部，无所谓断供 |
| **Token 消耗** | 正常 | 正常（断供期为零） | **恒为零** |
| **需要密钥** | 需要 | 需要（缺了就恒跑本地） | **不需要** |

**共同项**：CPU 2 核足够、闲时 < 1%；`/search` 单次 0.14~0.23s；冷启动 5.2s；数据盘千级记忆约 13 MB 向量 + 数百 KB SQLite；前端 0 依赖；Python 3.10–3.12。

**那 150 MB 花在哪、能不能省**（实测，不是估算）：onnxruntime 运行库只 import 不加载模型 **75 MB**；bge-small-zh-v1.5 会话与权重**约 122 MB**；两档服务实测差 **150 MB**（构成项单独实测、与档差有共享页，两个口径如实并列不做加法）。我们试过压它：`threads=1`、ONNX arena 按需分配、`malloc_trim`、`MALLOC_ARENA_MAX=2`——**四种旋钮实测全部无效**；模型已是 fastembed 目录里最小的中文可用款（次小的多语言方案是它的 2.4 倍）。所以我们没假装优化，而是给了你开关：**不需要备胎就选云端档，那 150 MB 一分不花。**

> **备胎为什么常驻**：双索引要求每一次写入都同步算一份本地向量——断供那一刻再加载模型也召回不到任何东西。备胎是提前备好的，不是临时找的。
> 其余的轻是刻意设计：向量库嵌入式落盘（无独立进程/端口）、不吃 GPU、相关性闸门先拦闲聊、SQLite+FTS5 兜底。一句话：**云端档 1 核 1G 跑得动；自动挡/本地档建议 2 核 2G 起步。**

> **按机器内存选挡（部署 Agent 请照此选）**：可用内存充裕（≥2G 余量）且要「云断了还能本地召回全部历史」→ 用 `auto`（本地备胎**常驻热备**，那约 174MB 就是买这份断网韧性）；机器内存吃紧或不需要断网兜底 → 用 `cloud`（约 280MB，云断则如实 `degraded`、不本地降级）。**别让内存紧张的机器默认跑 `auto` 再嫌重**——挡位是给你按机器余量选的，选前先 `free -m` 看余量。
>
> **规划中的冷备挡**：本地模型**不常驻**（平时省那约 174MB），仅在云端失效时才加载 + 批量补算存量本地向量；实测中小库（约 1500 条）补算窗口十几秒，期间 `degraded`、补完全量恢复。它兼顾「平时省内存」与「故障后全量韧性」，代价是故障后有一个补算窗口（时长随库大小线性增长，大库需增量补算）。这正面回应「自动挡该按需加载」——热备（当前 `auto`）与冷备（规划）的取舍会并入选挡指南。

## 控制台：记忆不再是黑盒

起服务后开 `http://127.0.0.1:8767/ui`：记忆浏览/搜索/调整、健康探针与挡位状态、联邦与演化、检索质量面板、**记忆档案一键导出**（Markdown，按出身分区、推断逐条标注「未经验证」）。顶部域选择器只显示服务端登记且 active 的 `(user_id, bank_id)`；失效域自动回退服务端默认域，目录不可用时不会猜一个 demo 域。联邦成员的 `profile` 只是展示分组，不等于记忆域。

## 接入面

| 宿主 | 方式 |
|---|---|
| Hermes Agent | MemoryProvider 插件：每轮自动记、自动召回（压缩前抢救） |
| Claude Code | CLI hook / MCP |
| Cursor | 规则文件（保存时自动入 Raw Drawer） |
| 任意 MCP 客户端 | MCP Server（41 工具，默认 :8766，stdio/HTTP 双传输） |
| 其他 | REST API（:8767） |

接入细节：[docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)。

## MCP Server（41 工具 · 默认端口 8766）

MCP 与 REST 同进程双栈：REST 在 :8767，MCP 在 :8766（stdio/HTTP 双传输）。**鉴权纪律**：非回环绑定必须配置 `AIDUMEM_API_TOKEN`，否则拒绝启动；确有公网暴露需求才显式设置 `AIDUMEM_ALLOW_INSECURE_PUBLIC=1`（默认关闭，开启会打 critical 日志）。工具分组与调用示例见 [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)。

## 🔐 安全模型

Bearer 令牌（`AIDUMEM_API_TOKEN`）+ 控制台口令（PBKDF2）+ 注入防护 + 默认仅回环。启动后先查三个数：`/health` 的 `health_status`、`degraded`、`probes.runtime_paths.data_dir_writable`（未带凭据时探针脱敏，附 `_redacted` 说明）。多轮外部安全审计留痕见 [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md)。

## 环境变量（关键项）

| 变量 | 作用 | 默认 |
|---|---|---|
| `AIDUMEM_API_TOKEN` | API 鉴权令牌（非回环强制） | 空=仅回环 |
| `AIDUMEM_ENTITY_KEYWORDS` | 实体词表（人名/项目代号，喂给相关性闸门） | 空 |
| `AIDUMEM_DATA_DIR` | 数据目录 | `~/.aidumem` |
| `AIDUMEI_ENGINE_MODE` | 引擎挡位 cloud/auto/local | auto |
| `AIDUMEM_CONFIG_READONLY` | 控制台配置只读演示模式 | 0 |
| `AIDUMEI_INJECT_DATE` | 召回注入带不带时间：`day`/`minute`/`off`（钩子侧） | day |

全量环境变量登记册见 `ducky/env_registry.py`（代码即真相源，错拼会启动告警）。

> **双前缀已冻结**：新变量一律 `AIDUMEI_`（`AIDUMEM_` 为 legacy，不再新增）。
> 存量变量不迁移（兼容红线），仅文档标注。

## 功能地图（一张表，不讲故事）

| 层 | 能力 |
|---|---|
| 检索 | bge-m3 向量 + FTS5 中文 BM25/trigram + cross-encoder 真重排；相关性闸门（闲聊不检索，省 token） |
| 记忆语义 | 三轨遗忘（身份永不衰减/情感加速/标准曲线）· 双时间轴（记忆**过期**而非删除）· 六型分类 |
| 治理 | 写入双审 + 冲突消解 + 注入防护；事件账本全路径留痕；密码学谱系（可检测篡改） |
| 进化 | 反思（主动/定时）· 本能升格技能（人工审批闸门）· 检索自进化反馈环 |
| 协作 | 联邦：多 Agent 共享一套记忆（MoE 门控 + 细粒度授权 grants）· 多 bot / 多 profile 各据一域，记忆人格独立、跨域默认隔离 |
| 周边 | 多模态视觉记忆 · 代码图谱 · 原文保真抽屉 · Obsidian 双链 |

## 测试与质量

**测试层级如实说明**
> 读表口径：每行「通过 · 跳过」相加等于**该形态、该日期**下 `pytest --collect-only` 的收集数；不同日期的行分母可以不同（树在长），以各行括注的日期为准。跳过数按「轴」解释（见 [docs/TESTING.md](docs/TESTING.md)），不是失败。

| 维度 | 现状 |
|------|------|
| 用例总数 | **2221**（`pytest --collect-only` 实测，2026-09-23，f0.1 本树）＝ **行为用例 2015（产品代码直测）+ 脚本/钩子行为 70 + 守卫用例 136（文档/口径/结构）**。三桶口径与名单见 `scripts/count_test_kinds.py`，可一键复算——头条不用混合数 |
| 独立开发机 | 2209 通过 · **12 跳过** —— **2026-09-23 收集口径**（f0.1 本树，Python 3.12；完整 extras + 模型缓存，只缺 Hermes 宿主） |
| 基础安装路径 | 1821 通过 · **25 跳过** —— 只装 `requirements.txt` + `requirements-dev.txt`（**2026-09-09 生产机干净 venv 实测**，Python 3.12） |
| 生产机沙箱 | 1967 通过 · **26 跳过** —— **2026-09-11 生产机实测**（本树 `de09794`，独立沙箱 venv：宿主源码在场、不带 `.env`、无 ruff/mcp/fastembed 等）；生产实机部署后 1983 通过 · 10 跳过（同树，宿主轴齐备） |
| 全轴齐备 | 1844 通过 · **1 跳过** —— **2026-09-09 生产机实测**（独立全轴 venv：工具、extras、宿主源码、模型缓存与公开 LoCoMo 数据集齐备；那 1 跳过为本树新增用例的条件轴） |
| 层级 | 以**模块级单元测试 + 源码级守卫断言**为主，`TestClient` 驱动的接口测试为辅 |
| 平台前提 | 全量套件按 **Linux/macOS（POSIX）**口径维护：`backup_gate` 轴要 POSIX shell；`/health` 的 CPU/RSS 指标走 `resource` 模块，非 POSIX 平台诚实置 `None` 不崩。Windows 未列为全量测试平台 |
| 语句覆盖率 | 约 51%（`ducky/` + 入口，`coverage` 实测） |
| 未覆盖 | 真实 mem0 / Qdrant 集成、真实 LLM 调用、并发压测 —— 这些依赖外部服务，由生产环境实机冒烟承担 |

```bash
# 全量回归
pytest tests/
# 编译检查
python -m compileall ducky api_server.py mcp_server.py
```

> **为什么要把 2209 和 1967 都写出来**：2209 是本树开发环境 2026-09-23 的收集口径（缺宿主 ×12）；1967 是生产机独立沙箱 2026-09-11 实测（`de09794`，宿主在场但沙箱缺多项可选轴）——两者的跳过轴不同，数字必须与环境、日期和测试树一起读。

> **这 12 条不是玄学，自己就能验**：十三条跳过轴（宿主、工具、可选依赖、模型文件）全部登记在册（[docs/TESTING.md](docs/TESTING.md)），`HERMES_SRC` 三态可控、两个方向都能复现：
>
> ```bash
> # 2026-09-14 实测：先装齐依赖、部署模型缓存，并让 AIDUMEI_BENCH_DATA_DIR 指向含 locomo10.json 的目录
> pip install -r requirements.txt -r requirements-dev.txt
> pip install "mcp>=1.0.0,<2" ruff nltk regex numpy fastembed
> python scripts/fetch_local_embed_model.py
> pytest tests/ -q -rs | tail -1                                 # 无宿主：2209 passed, 12 skipped
> HERMES_SRC=/path/to/hermes-agent pytest tests/ -q | tail -1    # 有宿主：2221 passed
> HERMES_SRC=none pytest tests/ -q -rs | tail -1                 # 装了宿主也强制关掉，照旧 2209 passed, 12 skipped
> ```
>
> 上面代码块里的 `有宿主：2221 passed` 要**十三条轴同时齐备**才拿得到，宿主只是其中一条 —— 别把「装上宿主」当成「全绿」。

> **跳过轴全量登记**（门控条数与实测逐行对账，改一条这里就红）：
>
> | 跳过轴 | 门控用例数 | 位点 |
> |--------|-----------|------|
> | 宿主 Hermes 源码 | 12 | `tests/test_hermes_plugin.py` 整份 |
> | git 工作区 | 1 | `tests/test_v20_brand_policy.py`（要 `git ls-files` 当比对基准） |
> | `scripts/backup_gate.sh` + POSIX shell | 8 | `tests/test_v19_4_1_backup_gate.py` 整份 |
> | `qdrant_client` 已安装 | 1 | `tests/test_v20_vector_bank_contract.py` |
> | LoCoMo 数据集已就位 | 1 | `tests/test_v20_locomo_official.py`（全量数据集扫描要真文件） |
> | `regex` 已安装 | 1 | `tests/test_v20_locomo_official.py`（拿 `regex` 给标准库 `re` 对拍） |
> | `numpy` 已安装 | 1 | `tests/test_v20_locomo_official.py`（拿 `numpy.mean` 给 `sum/len` 对拍） |
> | `nltk` 已安装 | 13 | `tests/test_v20_locomo_official.py` 与 `tests/test_v20_benchmarks.py`（官方 F1 的 PorterStemmer，换实现就不是官方口径） |
> | `git` 可执行文件在场 | 6 | `tests/test_v20_gitignore_guard.py` 整份（拿一个临时空仓当 ignore 判据，不碰本仓的 `.git`） |
> | `mem0ai` 已安装 | 20 | `tests/test_v20_mem0_patch_layer.py` 整份（补丁层疗法要真实基座在场；此前缺 mem0 是 20 条 ERROR 冒充真缺陷，现在诚实跳过） |
> | `fastembed` 已安装 | 1 | `tests/test_v20_2_autoshift.py`（自动挡备胎真模型测试；缺依赖诚实跳过，模型未部署时用例内二次跳过） |
> | `ruff` 已安装 | 3 | 静态规则守卫：F821/F811/F841；缺依赖时跳过，发布门禁仍会拦截 |
> | `mcp` extra 已安装 | 8 | MCP 导入面守卫 + 鉴权行为 + SSE 传输用例 + 检索 session 透传 |
>
> 生产机独立沙箱裸跑（宿主源码在场、不带 `.env`），实测跑出来是 1967 passed、26 skipped（2026-09-11，本树 `de09794`）——跳过轴不同，数字必须与环境、日期和测试树一起读。

## 安全与合规

MIT License。`SECURITY.md` + [docs/SECURITY-AUDIT-LEDGER.md](docs/SECURITY-AUDIT-LEDGER.md)：多轮外部安全审计逐条留痕（含我们驳回误报的理由）。MCP 层非回环绑定强制 token，否则拒绝启动。

## 已知边界（诚实声明）

- 需要嵌入与 LLM 服务（云端或本地备胎）——换来的是真语义检索与抽取质量；要「完全离线 + 亚毫秒」的极简场景，零依赖本地类工具更合适，这话我们不遮。
- **基准成绩是「试跑」不是定稿**：2/10 样本、裁判非 GPT-4o。正式打榜须全量复跑，[benchmarks/RESULTS.md](benchmarks/RESULTS.md) 如实登记。
- 时序推理仍是**已知短板**：根因已修但**未复跑验证**，上表「目标」列一栏都还不是成绩。
- 本地 lite 挡与云端挡的召回重叠率实测有限（差异全部明码标价写在 docs），auto 挡存在的意义正在于此。

## 文档

部署与运维：[🤖 Agent Guide](AGENTS.md)（Agent 唯一入口）· [docs/HEALTH.md](docs/HEALTH.md) · [docs/OPERATIONS.md](docs/OPERATIONS.md) · [TROUBLESHOOTING.md](TROUBLESHOOTING.md) · [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) · [docs/POSITIONING.md](docs/POSITIONING.md)（与同类对比，口径全部可复算）· [docs/BENCHMARKING-POSTURE.md](docs/BENCHMARKING-POSTURE.md)（跑分态度与口径）

## 已知例外与本版不覆盖

| # | 例外 | 说明 |
|---|------|------|
| 1 | 租户隔离是按租户收窄可见性 | 非互不信任客户的强隔离层；域契约见 `ducky/bank_contract.py`，边界详录 `docs/SECURITY-AUDIT-LEDGER.md`。 |
| 2 | `fetch_local_embed_model.py` 必须部署期执行 | 运行时零网络；`ducky/local_embed.py` 强制 `HF_HUB_OFFLINE=1`。取模后按 `scripts/local_embed_model_sha256.json` 逐文件 sha256 校验，不匹配即删除并非 0 退出。 |
| 3 | `capture_wave` 的 `entity_keywords` 漏配时零召回 | 无报错，需配置 `AIDUMEM_ENTITY_KEYWORDS`。详见 `ducky/pipeline/memory_gate.py`。 |

## 仓库结构

```text
aiduMEI/
├── AGENTS.md / llms.txt    # Agent 部署入口与文档索引
├── api_server.py           # 主入口（API + /ui 控制台托管）
├── ducky/                  # 业务逻辑（hot/ pipeline/ speed/ salience/ federation/ evolve_mem.py …）
├── frontend/               # 控制台（零构建纯静态；js/vendor/ 本地 echarts）
├── benchmarks/             # 评测协议（数据集/模型/judge/seed/哈希全部锁定）
├── tests/                  # 回归测试集（pytest）
├── prompts/install.txt     # 一行 Prompt 部署正典
├── docs/                   # 运维/健康/备份/容量/测试口径
├── scripts/                # e2e_smoke.py · check_hook_deployment.py · report.py 等
└── mem0_config_local.json  # 模型配置（gitignored，含密钥）
```

## License

MIT — 详见 [LICENSE](LICENSE)。

<p align="center">
  <sub>aiduMEI⚕爱嘟优忆思（曾用名 aiduMEM / duMem，历史版本与文档中保留）｜Powered by monkey²</sub>
</p>

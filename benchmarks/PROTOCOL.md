# aiduMEI 评测协议（v20.0，冻结版）

> 本文件是评测的**先验承诺**：数据、模型、judge、prompt、seed、哈希在跑分
> 之前锁定。任何改动都必须改这份文件并留下版本记录——没有"跑完再定规则"。

## 1. 数据集与许可证

| 数据集 | 官方来源 | 提交号（本协议冻结时上游 HEAD） | 许可证 | 规模 |
|---|---|---|---|---|
| LongMemEval | 代码 github.com/xiaowu0162/LongMemEval；**数据** HuggingFace `xiaowu0162/longmemeval` | 代码 `9e0b455f4ef0e2ab8f2e582289761153549043fc`；**数据修订号** `2ec2a557f339b6c0369619b1ed5793734cc87533` | MIT | 每文件 500 实例（`longmemeval_s` / `longmemeval_m` / `oracle`） |
| LoCoMo | github.com/snap-research/locomo | `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376` | **CC BY-NC 4.0**（仅限非商业评测） | 10 段长对话，QA 总数由装载器现场统计 |

- 数据文件**不进仓库**；存放于 `AIDUMEI_BENCH_DATA_DIR`（默认 `~/.aidumem/bench_data`）。
- `benchmarks/data_manifest.json` 进仓库，记录每个数据文件的 SHA-256
  **与取数时的上游提交号**（`source_commit`）。
- **哈希状态：LoCoMo 已登记**（`registered_at` 2026-08-23T08:07:25Z）——
  `sha256` 前 16 位 `79fa87e90f04081`、`size_bytes` 2805274、`source_commit`
  `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`；明细与规模实测见 §4.5。
  **LongMemEval 已取数、已登记**（`registered_at` 2026-08-23T16:21:13Z）——
  headline 文件 `longmemeval_s.json`，`sha256` 前 16 位 `08d8dad4be43ee20`、
  `size_bytes` 278025796、`source_commit`（HF 数据修订号）
  `2ec2a557f339b6c0369619b1ed5793734cc87533`。
- **oracle 文件的哈希钉在本协议、不进 manifest**：`register()` 对每个数据集只
  写一条 `manifest[dataset]`，而 `_check_formal_manifest` 会拿 `--data-path` 的
  实测哈希跟这一条对撞。headline 归 `longmemeval_s.json`（见 §3 「oracle 永不
  作 headline」），所以 oracle 若也塞进 manifest 只会覆盖掉 s、并让 formal 闸门
  认错文件。oracle 诊断跑在非 formal 模式，其字节在此落钉：
  `longmemeval_oracle.json`，sha256
  `821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c`，
  15388478 字节，同一数据修订号。
- **两个文件的 sha256 均与上游自证值逐字节相符**：HuggingFace 对 LFS 文件返回的
  `x-linked-etag` 就是内容 sha256，二者与本地实测完全一致——这比自己算一遍哈希
  更强，等于上游替我们背书「取到的就是它发出的字节」。
- `run.py --formal` 在 manifest 缺失或含 PENDING 时**拒绝启动**，这是代码
  强制的闸门，不是口头约定——闸门校验的是 `data_manifest.json` 里的值，
  不是本文这段话，所以改这段话动不了闸门。
- **提交号同样是闸门**：`download.py --register` 缺 `--source-commit` 直接拒绝
  登记（也不接受 tag / 分支名——会移动的标识当不了先验承诺），`run.py --formal`
  在 `source_commit` 缺失或为 PENDING 时拒绝启动。理由是可复核性：SHA-256 只能
  证明「我这次跑的就是这个文件」，证明不了「这个文件取自哪一版」。两个数据集的
  标注都在持续修，别人拿我们公布的哈希对不上时，没有提交号就分不清是「取错了
  版本」还是「文件被改过」——成绩也就无法被独立复核。
  上表的提交号是**冻结时的上游 HEAD，仅作参考基准**；真正进 manifest、进
  digest、有约束力的是登记那一刻记下的 `source_commit`。两者不一致不算违规，
  但必须能解释（例如上游在冻结后又修了标注）。
- LoCoMo 图片只有外部 URL：本管线不抓取、不缓存、不评测任何图片内容。
- 上游数据的已知标注问题（如部分 cat5 只有 `adversarial_answer`、evidence
  引用缺失）由 schema 校验器**如实上报**进 manifest 的 `schema_report.anomalies`；
  原始数据一个字节不改。若评分需要修正，走版本化 correction manifest 并做
  敏感性分析（含/不含修正各报一遍）。

### 1.1 修正清单（correction manifest）

修正清单放在 `benchmarks/corrections/*.json`，由 `benchmarks/corrections.py`
装载校验，`run.py --formal --corrections <清单>` 使用。规则如下，全部由代码
强制，不是口头约定：

| 约束 | 具体要求 | 违反时 |
|---|---|---|
| 版本号 | 必须有 `manifest_version` | 拒绝装载 |
| 哈希钉 | 清单**非空**时 `applies_to_sha256` 必须等于本次数据哈希 | 拒绝装载 |
| 动作白名单 | 只允许 `add_evidence`、`mark_adversarial` | 拒绝装载 |
| 不许改正文 | 出现 `answer` / `question` / `adversarial_answer` / `text` 键 | 拒绝装载 |
| 必须写理由 | 每条修正都要有非空 `why` | 拒绝装载 |
| 不许静默失效 | 修正匹配不到目标（下标越界 / dia_id 不存在 / 类别不符 / 已生效） | 拒绝运行 |
| 敏感性分析 | 非空清单必须配 `--sensitivity-baseline`（同一份数据的零修正 formal summary） | 拒绝启动 |

三点设计交代：

- **为什么只许重述标注**：补一条上游漏标的 evidence 引用、把官方口径的拒答题
  标出来，改的是「上游少写了什么」；改 `answer` 正文改的是「正确答案是什么」，
  那不是修正，是造数据。所以白名单是白名单，不是黑名单——新动作必须显式加进
  代码并配上校验，加不进来就用不了。
- **为什么空清单不用钉哈希**：空清单改不动任何数字，钉了没有意义。门槛正好
  落在「能改动数字」的那一刻：加入第一条修正，哈希钉立刻成为硬性要求。
- **为什么修正藏不住**：修正块写进 summary 的 `config`，而 `config` 进
  digest（§5）。应用了修正，digest 必变——不存在「悄悄改了分数」的路径。

当前状态：`benchmarks/corrections/locomo_v0.json` 是**空清单**（0 条修正）。
LongMemEval 无清单。

## 2. 系统配置（被测方）

- 被测对象：**正在运行的 aiduMEI HTTP 服务**（真实网络栈，非进程内捷径）。
- 检索入口：`POST /search`（显式搜索；不经过 `/gate` 门控）。
- 写入入口：`POST /add`，每请求 `metadata.force_sync=true`；若服务仍返回
  `accepted`，适配器轮询 `/add/job/{job_id}` 直到 `done`——2xx 回执不算写成功。
- case 隔离：`user_id = bench-<dataset>-<sha256(case_id)[:16]>`、
  `bank_id = bench-<sha256(case_id)[:16]>`，双重命名空间；题目原文不进标识符。
- top-k：默认 5（`--top-k` 可调，调了必须写进运行 summary）。
- **无 benchmark_mode**：适配器不改变任何生产门控行为。
  - v20.0 补注：`/add` 的 `infer` 是**公开契约字段**（`ducky/api_models.py`），
    不是隐藏模式。区别在可验证性：隐藏模式是「适配器偷偷改了被测系统的
    行为」，公开参数是「调用方要求了什么、回执里写着什么」——服务端必须
    回显 `infer:false`，否则适配器抛 `protocol` 错并拒绝继续。仓内
    `ducky/speed/pipeline.py` 的 fastpath 早就在用 `infer=False`，`/add`
    只是此前从未暴露。**`--formal` 一律 `infer=true`，与生产完全同路。**

### 2.1 灌注消息的 role 契约（2026-08-23 修订 4 定）

`POST /add` 的 `messages[].role` **只许是 `system` / `user` / `assistant`**。
数据集里的说话人名（LoCoMo 的 `speaker`，如 `Melanie` / `Caroline`）
**不是 role**，一律按下述规则归一，落点在 `benchmarks/adapter.py::add_turn`：

| 入参 role | 线上 role | 线上 content | 元数据 |
|---|---|---|---|
| `system`/`user`/`assistant` | 原样 | **原样，不加前缀** | 不加字段 |
| 其他（说话人名） | `user` | `"<说话人>: <原文>"` | `bench_speaker=<说话人>`，且 `messages[0].name=<说话人>` |

**为什么必须这样**：服务端 mem0 抽取层 `parse_messages()` 只认那三个分支，
**没有 else 也没有告警**。role 不在白名单 → 抽取提示词是空串 → 零事实入库，
而 `/add` 照回 `status: ok, action: new`。实测灌 78 轮、烧掉 77 次抽取 LLM
调用与 316 次嵌入调用，语义向量库属跑分租户的点数仍是 **0**，逐字库却完全
健康——**分数跑得出来，但测的只是逐字库那一半**。四组对照见
`FINDING_role_drop.md`：`user` 原句 Δ=+1；`Caroline` Δ=0；同内容改第一人称
仍 Δ=0；`assistant` 也 Δ=0（故「A→user／B→assistant」这条修法**实测不成立**，
只有 `user` 能产出语义记忆）。

**为什么名字要进正文**：抽取层唯一读得到说话人身份的地方就是正文。
不前缀，两个说话人的事实会糊成一团，归属类问题必错——那是另一种假成绩。
`name` 字段与 `bench_speaker` 是给逐字库与审计留的存根，抽取层不读它们。

**合法 role 一个字都不许改**：G3b 的 bit 复现建立在字节形态稳定上。
归一若顺手给合法 role 的正文加了前缀，确定性断言就成了空话。
守卫：`tests/test_v20_benchmarks.py` 四条用例（两条归一 + 两条透传负向对照），
撤掉修复时归一那两条必须变红——已做注入式负向对照验证。


## 3. 防泄漏与诚实边界

1. LongMemEval：只灌注日期 ≤ `question_date` 的会话；之后的会话强制排除
   并逐条记录在 `excluded_sessions_after_question_date`。
2. 只有 `/search` 返回的证据才可交给答案模型；**未检索到任何证据时不作答**
   （abstain）——有负向测试钉死（`tests/test_v20_benchmarks.py`）。
3. `/search` 的组件故障（HTTP 200 + body `status:"error"`）按故障处理并抛错，
   **绝不**静默当作"没搜到"。空结果是合法结果，但单独计数（`empty_results`）。
4. 失败分类分别计数：401/403、其他 4xx、5xx、超时、协议不符、组件故障、
   job 失败、空结果——没有任何一类会被折叠成"成功但零条"。
5. oracle 文件只作检索上限诊断，**永不**作为 headline 成绩。
6. smoke 的 `evidence_recall_diagnostic` 是检索链路诊断分，
   **不是** LongMemEval/LoCoMo 官方指标，不得对外宣称。

## 4. 官方评分（QA 指标）

- LongMemEval：官方 `evaluate_qa.py`，judge 模型 **gpt-4o-2024-08-06**，
  temperature **0**。abstention 实例以 `question_id` 的 `_abs` 后缀识别。
- LoCoMo：官方评分脚本已移植为 `benchmarks/locomo_official.py`，逐项对齐与
  唯一一处偏离见 §4.1～§4.5。
- **答案模型、prompt 模板、seed 于 2026-08-23 在此锁定**，§4 原先「锁定前
  formal 不得开跑」的自封锁随之解除。锁定发生在**任何正式成绩产生之前**
  （formal 一次未跑）——这是本文档作为先验承诺的意义所在。

### 4.1 cat5（对抗/拒答）判分口径，与唯一一处偏离官方

- **判分**：cat5 **不计 F1**。模型答案里出现 `no information available` 或
  `not mentioned` 才得 1.0，否则 0.0（`locomo_official.score_one`）。官方选项
  文本 `Not mentioned in the conversation` 原样保留。
- **出题——唯一一处偏离**：官方脚本读 `qa['answer']` 造「诱答」选项。但公开
  发布的 `locomo10.json` 里 1986 题中有 **444 道 cat5 根本没有 `answer` 键**
  （只有 `adversarial_answer`），照抄官方会直接 KeyError 打死 **22.5%** 的
  考题。另有 **2 道两个键都有**，且 `adversarial_answer='Yes'` /
  `answer='No'`——诱答是前者。因此取值优先级定为 **`adversarial_answer` 在前、
  `answer` 兜底**（`locomo_official._cat5_distractor`）。
- 两个键都缺则**抛错，不静默造一个选项**：cat5 的两个选项就是题面，编一个
  出来等于自己改考卷。
- 字段普查（登记时全量扫过，结论进 manifest）：444 道只有
  `adversarial_answer`、2 道两键并存、1540 道只有 `answer`。

### 4.2 答案模型 LOCKED

| 档位 | 网关 | 模型 ID | 成绩效力 |
|---|---|---|---|
| dry（抽样试跑） | 9r | `gemini-3.7-flash` | **仅供内部看趋势，不得对外宣称** |
| formal（全量正式） | 中转 | `gpt-4o-by-openai` | **唯一可引用的成绩** |

- **生成参数**：`temperature=0`、`max_tokens=32`，与官方
  `run_chatgpt(..., num_tokens_request=32, temperature=0)` 一致。代码里
  **不重打这两个数**，而是从 `locomo_official.OFFICIAL_TEMPERATURE` /
  `OFFICIAL_MAX_TOKENS` 引用——抄一遍就会有两份真相。
- **为什么 formal 用中转的 gpt-4o**：可比性。公开可比的 LoCoMo 成绩用的是
  OpenAI 系答题模型，换成别家模型，分数就只能跟自己比。dry 走 9r 是为了省
  中转额度。
- **dry 档为何从 `qwen3.8-max` 换成 `gemini-3.7-flash`**（2026-08-23 定档）：
  两条实测硬伤。① **线路拥挤**：约每三次一次 HTTP 521。② **协议不兼容**：它是
  推理模型，官方 32 token 上限会被 `reasoning_content` 吃光，`content` 返回
  `null`、`finish_reason=length`；且**时好时坏**（同一提示词、同样 temperature=0，
  多次调用有的出词有的空）——间歇性比稳定失败更毒，它只会静默压低分数而不报错。
  `gemini-3.7-flash` 实测 32 token 下 `finish_reason=stop`、无思考字段污染，
  且隐藏指令 ≈2118 tokens，比 `claude-opus-5` 的 ≈8941 少挤占四倍答案预算。
  **formal 档不动**——可比性口径见下，正式成绩仍只认中转 `gpt-4o-by-openai`。
- **路由是白名单**：`answerer.ROUTES` 逐个模型登记网关，没登记的模型直接
  报错，不做前缀猜测的兜底——走错网关＝烧错额度。
- **9r 通道有一段无法关闭的隐藏 system prompt**（2026-08-23 实测：
  `qwen3.8-max` ≈2053、`gemini-3.7-flash` ≈2118、`claude-opus-5` ≈8941 tokens；中转
  `gpt-4o-by-openai` 实测 16 tokens，干净）。它只会**压低** F1（挤占 32
  token 的答案预算、掺入与考题无关的指令），所以 9r 上跑出来的数只能当
  **保守下界**。这条差异必须随 dry 成绩一起公布，不许只报数字。
- **重试是必需而非优化**：`qwen3.8-max` 实测约每三次一次 HTTP 521。重试次数
  与最终失败都记进 summary，`answer_failures > 0` 时聚合器**拒绝**给出均值。
- **网关地址与密钥同级敏感，一律不进仓库**：两者都从环境变量或本机钥匙串
  读取，取不到就报错，**没有任何字面量兜底**；并由守卫
  `test_源码里不许出现网关地址字面量` 钉死（该守卫做过注入式负向对照：
  注入即红、复原即绿且文件 SHA-256 不变）。

### 4.2.1 dry 档抽样口径（`--max-samples` / `--max-qa`）

- **`--max-qa` 会把成绩取偏，默认不要用。** LoCoMo 每个样本的 `qa` 数组是
  **按 category 排序**的，`--max-qa N` 取的是「前 N 题」而非随机 N 题。以
  `sample[0]`（199 题，类别分布 1:32 / 2:37 / 3:13 / 4:70 / 5:47）实测：

  | 取前 N 题 | 覆盖到的 category |
  |---|---|
  | 20 | 1, 2, 3 |
  | 30 | 1, 2, 3 |
  | 50 | 1, 2, 3 |

  也就是说 N 小于约 120 时，**占比最大的 category 4（70 题）和对抗题
  category 5（47 题）会被整类切掉**——切掉的恰好是最难的两类，剩下的数
  只会偏高。要限规模就限 `--max-samples`（按样本切，类别分布完整），
  **不要限 `--max-qa`**。若确实用了 `--max-qa`，公布时必须连类别覆盖一起报。

- **灌注是瓶颈，不是答题。** 实测约 **4.1 轮对话/分钟**（2026-08-23 沙箱实
  测：10 分 19 秒灌 42 轮；每轮都要过一次抽取 LLM）。`sample[0]` 419 轮
  ≈ 1 小时 42 分；全量 10 样本 5882 轮 ≈ 24 小时。排期按这个量级算，别按答题
  次数算。**早先写的「6～7 轮/分钟」是估值，已被实测推翻，勿再引用。**

- **跑分必须脱离启动它的进程组**（macOS 无 `setsid`，用
  `subprocess.Popen(..., start_new_session=True)`）。`nohup` 只挡 SIGHUP，
  挡不住 SIGINT：同组的任何一次前台中断都会连坐掐死几小时的跑分（已犯过）。

### 4.3 prompt 模板 LOCKED

- 三段模板与官方**一字不差**移植（`benchmarks/locomo_official.py`）：
  - cat2 追加后缀 `" Use DATE of CONVERSATION to answer with an approximate date."`
    （**含句末句号**）。
  - cat5 选择题模板 `" Select the correct answer: (a) {} (b) {}. "`
    （**含末尾那个空格**）。
  - `CONV_START_PROMPT` 保留官方原文的拼写错误 **`wriiten`**；RAG 模式下官方
    并不使用这一段，我们也不用，但原样留档以便对齐。
- 答案抽取：`get_cat_5_answer` 按官方口径松判——长度 1 看是否含 `a`、长度 3
  看是否含 `(a)`，其余原样返回。
- **与官方不同、必须写明的一处：检索单元。** 官方 RAG 基线检索的是对话原文
  拼成的 `X said, "..."`（有图再拼 ` and shared <blip_caption>`）；我们检索的是
  **被测系统自己写入的记忆条目**——这正是被测对象本身，不是可以对齐的自由度。
  因此本管线的 LoCoMo 成绩是「记忆系统」成绩，**不是官方 RAG 基线的复现**，
  引用时必须带着这句话一起引。

### 4.4 seed LOCKED

- CLI `--seed`，**默认 0**。
- **唯一消费者**：cat5 的 (a)/(b) 选项顺序。官方用的是没设种子的
  `random.random() < 0.5`；答题模型对选项顺序敏感，不锁种子就谈不上可复现。
- **派生方式**：按 `f"{seed}:{sample_id}"` 取 SHA-256 前 8 字节作
  `random.Random` 的种子——每个 sample 独立可复算，不受遍历顺序影响。
- 每份 summary 都记 `cat5_option_seed`，并进 digest。

### 4.5 数据集指纹与上游异常

- `benchmarks/data_manifest.json` 已登记 LoCoMo 与 LongMemEval（headline 文件
  `longmemeval_s.json`；oracle 的哈希钉在 §1，不进 manifest）。
- **规模实测**：10 段长对话、5882 轮、QA 总数 1986；分类计数
  cat1 282 / cat2 321 / cat3 96 / cat4 841 / cat5 446（合计 1986，对得上）。
- `schema_report.anomalies` 如实记录 **9 处上游标注异常**——evidence 指向不
  存在的 `dia_id`（形如 `'D8:6; D9:17'`、`'D'`、`'D:11:26'`、`'D30:05'`）。
  **原始数据一个字节不改**，这 9 处不做修正、只登记；它们只会让
  `evidence_hits` 这类诊断指标偏低，不影响 F1 主指标。
- 跑分产物（`benchmarks/runs/`）含数据集原文片段，已 gitignore，**不进仓库、
  不对外发布**；对外只发 `RESULTS.md` 的聚合数字与配置指纹。

## 5. 运行留证（每次运行必产出）

- JSONL 原始记录：逐实例的注入会话、排除会话、**检索结果原文**
  （`retrieved_evidence_only`，两个数据集都有）、证据命中（`evidence_hits` /
  `evidence_sessions_hit`）、**每条命中的判据**（`evidence_hit_basis`）、
  request 计数。
- summary JSON：配置全量（数据哈希、top-k、fixture/data 指纹）、失败分类
  统计（adapter.stats）、确定性 **digest**。
- digest 对确定性内容取 SHA-256，剔除三类字段：① 时间戳/延迟/request_id；
  ② 嵌套簿记字段（`id` / `memory_id` / `hash` / `created_at` /
  `updated_at` / `recorded_at`）；③ **模型派生浮点**（`score` /
  `_hybrid_score` / `_time_decay`，理由见 §8 修订 2）。
  **留在 digest 里的**：检索结果的成员、顺序、正文、离散排名
  （`_bm25_rank`）、证据命中集合、`would_answer`——即"检索结果变了"
  照样会被 digest 抓住。剥掉的浮点改由 `compare_runs.py` 按显式容差单独
  检查，并报出实测噪声地板。
- 召回诊断：**无证据的题记 N/A（`null`），不记 0.0**；`recall_aggregate`
  只在适用题上取均值。`evidence_recall_applicable` 是机器可读的判别位。
- 证据命中的判据（`evidence_hit_basis`）逐条如实标注是 `metadata`（元数据
  精确回指）还是 `verbatim_text`（**灌进去的那一串精确原文**，仅压空白/
  统一大小写）。改写、翻译、摘要一律不匹配——这是身份判定，不是语义给分。

### G3 复现性闸门（两档，v20.0 修订——见 §8）

| 闸门 | 写入通路 | 断言 | 用途 |
|---|---|---|---|
| **G3a** | 生产同路（`infer=true`，LLM 抽取在环） | **结构不变量**跨两遍一致：`records_total`、`data_report`、全部失败分类计数（`adapter.stats`）、每条记录的 `would_answer` / 检索空非空 / 期望证据 / 证据命中 / 召回诊断。digest **允许不同**。 | 正式跑分前的管线自检；formal 只用这一档 |
| **G3b** | `--deterministic`（`infer=false`，LLM 出环，规范化原文直写） | 上述结构不变量 **＋ digest bit 相同**；模型派生浮点按显式容差（默认 `5e-3`）另查，并报出实测噪声地板。 | 证明「跑分器自身」确定：同输入→同字节输出 |

- **闸门是可执行的**：`benchmarks/compare_runs.py`（只用标准库，退出码
  0/1/2）。判定不靠人眼比对——眼看会漏、会自我说服，也进不了 CI。
- **反假绿硬条件**（任一不达标即 FAIL，绝不报绿）：记录数 > 0；**并非全部**
  `retrieved_count = 0`；适用题的证据命中**不得全空**；失败分类计数一个都
  不许漂（`requests` / `retries` 除外，重试是环境噪声）。
- **部分空结果只警告不判红**，并把「几条有实质结果 / 其中几条是拒答题」
  写进报告与 stderr。空结果是 §3.3 承认的合法结果，cat5 拒答题的空更是
  §4 规定的正确行为；拿它判红会让闸门因数据难度长红，而一条长红的闸门
  等于没有闸门（与拆分 G3a/G3b 同一条理由）。弱证明力必须**写明**，不许
  静默吞掉，也不许当成通过的资本。
- 两档的 `config.gate` / `config.write_path` 都进 digest，因此两种模式的
  digest 不可能相撞；`compare_runs.py` 另有一道硬拦：运行自称的闸门与
  `--gate` 不符时直接判失败，无法拿 G3b 的哈希冒充 G3a 通过（有负向测试
  钉死）。
- `--formal` **拒绝** `--deterministic`（argparse 层硬拒）：正式成绩必须与
  生产同路，免抽取直写是另一个系统的成绩。
- G3b 的召回数字同样不得对外宣称（理由同 §3.6，且免抽取语义与生产不同）。

## 6. 重试语义的已知取舍

`/add` 非幂等。适配器对 5xx/超时做有限重试（默认 2 次），极端时序下可能
造成重复写入。评测语境下重复记忆只会让检索**更难**而非更容易，不会虚增
成绩，故接受这一偏保守取舍并在此如实记录。`/search`、`/health`、job 轮询
天然幂等，重试无副作用。

### 6.1 空答案不许当「答错」（2026-08-23 修复）

`AnswerModel.complete()` 原先把 `content: null` 用 `or ""` 兜成空串**直接
返回**。这条路会造假绿灯：空答案判错、摊进均值压低成绩，而
`answer_failures` 仍记 0——**没有任何报错**。推理模型在官方 32 token 上限
下正是这个形态（预算被 `reasoning_content` 吃光，`finish_reason=length`）。

已改为：`strip()` 后为空即视作瞬时故障重试；重试到上限仍空则抛
`AnswerError`，落进 `answer_failures`。这样两种情形都不会静默记成答错——
间歇性空能被重试捞回来，协议不兼容会当场炸。覆盖用例四条（null / 纯空白
负向对照 / 瞬时空重试捞回的正向对照 / 空 choices）。

**口径影响**：修复前启动的运行不带此保护（Python 不热重载）。凡在
2026-08-23 18:35 之前启动的 dry 运行，读 summary 时**必须**独立地自数
一遍 `prediction_raw` 为空的条数，不能只看 `answer_failures == 0`。

## 7. 复现命令

```bash
# G3a：生产同路自检（跑两遍，比对结构不变量；digest 允许不同）
.venv/bin/python -m benchmarks.run --smoke --dataset locomo --base-url http://127.0.0.1:8100 --out-dir /tmp/g3a_A
.venv/bin/python -m benchmarks.run --smoke --dataset locomo --base-url http://127.0.0.1:8100 --out-dir /tmp/g3a_B
.venv/bin/python -m benchmarks.compare_runs --gate g3a /tmp/g3a_A/*_summary.json /tmp/g3a_B/*_summary.json

# G3b：免抽取复现性自检（跑两遍，digest 必须 bit 相同）
.venv/bin/python -m benchmarks.run --smoke --deterministic --dataset locomo --base-url http://127.0.0.1:8100 --out-dir /tmp/g3b_A
.venv/bin/python -m benchmarks.run --smoke --deterministic --dataset locomo --base-url http://127.0.0.1:8100 --out-dir /tmp/g3b_B
.venv/bin/python -m benchmarks.compare_runs --gate g3b /tmp/g3b_A/*_summary.json /tmp/g3b_B/*_summary.json
# 判定看退出码：0 通过 / 1 断言失败 / 2 用法或文件问题。
# 注意：别把闸门命令接管道——管道会把退出码换成管尾命令的退出码，红灯会被吞掉。
# longmemeval 同理，把 --dataset 换掉即可。

# 数据登记（手工获取后锁哈希 + 锁上游提交号；缺 --source-commit 拒绝登记）
.venv/bin/python -m benchmarks.download --register longmemeval longmemeval_s.json \
  --source-commit "$(git -C /path/to/LongMemEval rev-parse HEAD)"
.venv/bin/python -m benchmarks.download --show

# formal（哈希闸门通过后才会启动；--deterministic 在此被硬拒）
.venv/bin/python -m benchmarks.run --formal --dataset longmemeval \
  --data-path "$AIDUMEI_BENCH_DATA_DIR/longmemeval_s.json" --base-url http://127.0.0.1:8100

# 带修正的 formal（§1.1）：先跑零修正基线，再拿基线 summary 做敏感性对照。
# 顺序反了不行——非空清单没有基线一律拒绝启动。
.venv/bin/python -m benchmarks.run --formal --dataset locomo \
  --data-path "$AIDUMEI_BENCH_DATA_DIR/locomo10.json" --base-url http://127.0.0.1:8100
.venv/bin/python -m benchmarks.run --formal --dataset locomo \
  --data-path "$AIDUMEI_BENCH_DATA_DIR/locomo10.json" --base-url http://127.0.0.1:8100 \
  --corrections benchmarks/corrections/locomo_v0.json \
  --sensitivity-baseline benchmarks/runs/formal_locomo_<戳>_summary.json
```

## 8. 修订记录

本文件是**先验承诺**，其全部价值在于「不能看到结果再改规则」。因此每一次
改动都必须在此留痕，包括——尤其包括——在看到失败之后做的改动。

### 修订 1（v20.0 开发期，跑分前，正式数据尚未登记）

**改了什么**：把 §5 原本单一的 G3 闸门（"同一配置跑两遍，digest 必须一致"）
拆成 G3a（结构不变量）+ G3b（免 LLM 的 bit 复现）；digest 的波动字段剥离
补齐嵌套簿记字段；无证据题的召回诊断由 0.0 改为 N/A。

**为什么改（诚实交代）**：这是**在观察到 G3 失败之后**提出的修订，不是事前
设计。发现过程与结论：

1. longmemeval smoke 连跑两遍，digest 不同（`ff500be9…` vs `08c429ca…`）。
2. 补齐嵌套簿记字段剥离后**仍然不同**（`01aa7945…` vs `ea0039e0…`）。
3. 逐字段定位残差：差异是 LLM 抽取出的 `memory` 正文本身，以及由它派生的
   `score` / `_hybrid_score` / `_bm25_rank`。远程 LLM 即便 temperature=0
   也不保证逐字节可复现（服务端批处理、浮点归约顺序、模型热更新）。
4. 结论：**含远程 LLM 的链路上「两遍 bit 相同」原理上不可达**。原 G3 不是
   一条被违反的高标准，而是一条无论系统多好都永远红的断言。

**为什么这不是放水**：原 G3 事实上无法通过，一条永红的闸门等于没有闸门；
拆分后两档都是**可通过且会真失败**的断言——G3a 会因失败计数漂移或记录数
变化而红，G3b 会因写入通路引入任何非确定性而红。此外新增了一条原先没有的
硬约束：服务端不回显 `infer:false` 时适配器直接抛错，即"未经证实的确定性
一律不接受"。

**同期发现的真 bug（不是标准问题，是管线没接上）**：LoCoMo 的 `dia_id`
从未灌进元数据，而证据匹配器正是拿它去召回结果里找——`evidence_hits`
因此**结构性恒空**、召回诊断恒 0.0。此前 locomo 的 digest 两遍相等是
**假绿**：3 条记录里 2 条为空，run B 吞掉 3 次超时 + 3 次重试仍未扰动
digest。已修（`adapter.add_turn(dia_id=...)` + 元数据感知匹配器）。

**批准**：v20.0 发布前由项目负责人明确点头，修订与实现同一提交落地。
**时点**：正式数据 `sha256` 仍为 PENDING、formal 一次未跑——即本次修订
发生在**任何正式成绩产生之前**，不存在按成绩反向调规则的可能。

### 修订 2（v20.0 开发期，跑分前，正式数据仍为 PENDING）

**改了什么**（五项，都在 §5 / §7 落了对应条目）：

1. digest 再剥一层：**模型派生浮点** `score` / `_hybrid_score` /
   `_time_decay` 不进 digest；成员、顺序、正文、离散排名 `_bm25_rank`
   照旧进。剥掉的数值改由 `compare_runs.py` 按显式容差（默认 `5e-3`）
   单独检查，并**报出实测噪声地板**。
2. LoCoMo 记录补上 `retrieved_evidence_only`——§5 一直承诺 JSONL 里有
   "检索结果"，而 LoCoMo 侧此前根本没写。
3. 闸门变成可执行程序 `benchmarks/compare_runs.py`（退出码 0/1/2），
   §7 的复现命令改为调用它。
4. 证据匹配器认两种判据，并把判据逐条写进 `evidence_hit_basis`；
   删掉原先"证据 id 出现在整条记录 JSON 里就算命中"的子串兜底。
5. 反假绿的红/警告分界写死：全空判红，部分空只警告（见 §5）。

**为什么改（诚实交代，仍然是"看到失败之后"）**：

1. 补上第 2 项之后 **G3b 立刻变红**（locomo `72ebe2da…` vs `4f2e4efc…`）。
   逐字段定位：差异**只在** `score` / `_hybrid_score`，实测
   |Δ|≤7.931e-04（locomo，24 个数值）、≤2.411e-04（longmemeval，12 个）。
2. 这条比修订 1 更该记一笔：**即便 LLM 已经出环**（`infer=false`），
   数字仍然抖——因为 **embedding 也是远程服务**，服务端批处理与浮点归约
   顺序同样不保证逐次一致。修订 1 只归因到 LLM，这里补正：远程模型
   **任何**一环在环，浮点都不可逐字节复现。
3. **为什么不用四舍五入糊过去**：实测抖动 ~6e-4，恰好能在小数第 3 位的
   分桶边界上翻面。那会造出一个**间歇性红灯**——比没有闸门更坏，因为它
   训练人忽略红灯。正确做法是把浮点从"必须逐字节相同"的断言里拿出来，
   换成"容差内 + 公布量到的噪声地板"，让不确定性**可见且有数**。

**为什么这不是放水**（三条自查，都有负向测试钉死）：

- 剥的只是**数值**：顺序变、正文变、条数变、`_bm25_rank` 变，digest 一律
  变红（`test_digest_ignores_model_derived_floats_but_keeps_ordering`）。
- 容差是**会真失败**的：6e-2 的偏移必须红
  （`test_gate_score_tolerance_fails_beyond_threshold`）。
- 这次同时**收紧**了两处：删掉子串兜底（证据 id 出现在无关字段里也算
  命中，是虚增通道）；新增"拿 G3b 结果冒充 G3a"的硬拦
  （`test_gate_rejects_impersonating_the_other_gate`）。

**同期发现的真问题（产品侧，未在本次修改产品代码）**：生产同路
（`infer=true`）下 LoCoMo 有一题的正确轮次**确实被检索到了**，却因为走的是
verbatim 召回路径而**判为 0.0**——那条路径回来的 item 没有 `metadata`
字典。根因：`verbatim_turns` 表**没有 metadata 列**，自定义元数据从未落库
（另有 `store_verbatim` 只认 `session_id`/`conversation_id`，读不到
`bench_session_id`）。这是**假红**：会让人以为检索坏了，去"修"一个没坏的
东西。本次只在**测量侧**修（原文精确回指 + 判据留痕），召回诊断
0.0 → 0.5；**没有**把一次生产库结构迁移夹带进这个改动里。

**遗留的已知局限（如实登记，不装作已解决）**：verbatim 召回路径**无法**
提供元数据级溯源，只能靠精确原文回指；因此改写/翻译/摘要类命中在该路径上
**必然漏计**（宁可漏计也不放宽匹配）。是否给 `verbatim_turns` 加
metadata 列属于生产库变更，需项目负责人单独点头，不在本次范围内。

**时点**：正式数据 `sha256` 仍为 PENDING、formal 一次未跑。本次修订同样
发生在任何正式成绩产生之前。修订后四档闸门实测全绿（退出码 0）：
G3a longmemeval（digest 不同，符合预期）、G3a locomo、G3b longmemeval
（`bbe0e451…` 两遍相同）、G3b locomo（`20304c6f…` 两遍相同）。

### 修订 3（v20.0 开发期，formal 跑分前，LoCoMo 数据已登记）

**改了什么**（三处，都在本文档内）：
1. §1「哈希状态：PENDING——数据尚未登记」改为已登记事实（`sha256` 前 16 位
   `79fa87e90f04081`、`size_bytes` 2805274、`source_commit` `3eb6f2c5…`、
   `registered_at` 2026-08-23T08:07:25Z），并明说 LongMemEval 仍未取数。
2. §4 解除「答案模型、prompt 模板、seed：PENDING」自封锁，新增
   §4.1～§4.5，把口径、模型、模板、seed、数据指纹逐条钉死。
3. 顺手修正 §4 原来那句不精确的 cat5 描述（原文写成「只有
   `adversarial_answer` 的题以拒答判定」，实际规则是取值优先级：
   `adversarial_answer` 在前、`answer` 兜底、两者都缺则抛错）。

**为什么改（诚实交代）**：§1 那句在数据登记完成之后就变成了**假话**，与同一
仓库里的 `data_manifest.json` 直接矛盾；§4 那句自封锁的用途是「跑分前必须
先锁配置」，配置现在锁完了，留着它反而会让人以为还没锁。两处都是**状态过
期**，不是标准放宽。

**为什么这不是放水**：
1. `--formal` 的 PENDING 闸门校验的是 `data_manifest.json` 里的值，**不是本
   文档的散文**；改散文一个闸门都动不了，这一点已写进 §1。
2. §4 是**锁得更紧**而不是更松：原来只有一句「待锁定」，现在把模型、网关、
   温度、token 上限、三段模板的逐字符细节、seed 派生方式、数据指纹全部写成
   可核对的先验承诺，还把「9r 有隐藏 system prompt、其成绩只是保守下界、
   不得对外宣称」这条不利于自己的事实一起写了进去。
3. 唯一一处偏离官方（cat5 诱答字段优先级）**明写在 §4.1**，连「照抄官方会
   KeyError 打死 22.5% 考题」的原因一起交代，不藏在代码注释里。

**同期发现的真 bug／真问题**：新写的答题客户端 `benchmarks/answerer.py` 里，
**网关地址曾以字面量硬编码在源码中**——地址与密钥同级敏感，这等于把私有端点
写进了准备开源的仓库。已改为从环境变量或本机钥匙串读取、取不到即报错、无字
面量兜底，并新增守卫钉死「源码里不许出现网关地址字面量」（做过注入式负向
对照）。更难看的是：**这次脱敏补丁本身又引入了一个新的敏感词**（我在新写的
注释里写了昵称），重扫后命中数一度从 1 变成 2，说明「改完必须重扫」不是形
式主义。已改为中性措辞并复扫归零。

**遗留的已知局限（如实登记）**：LoCoMo 与 LongMemEval 的数据哈希均已锁定，
但 §4 里 LongMemEval 的 judge 口径仍是照抄上游的声明，**未经本机实证**——
锁哈希只证明「跑的是这个文件」，证明不了「我们的 judge 判法跟上游一致」，
这一条要等本机正式跑一轮、拿到逐题判定后才能改口。另：`benchmarks/runs/` 里的原文片段不外发，因此外部第三方无法从
我们公布的材料完全复算逐题得分，只能复核聚合数字与配置指纹——这是数据集
许可（CC BY-NC 4.0）与诚实边界的取舍，不是技术障碍。

**批准**：v20.0 发布前由项目负责人明确点头（「先跑 LoCoMo，先不要贴到
README 里」——锁配置是跑分的前置条件）。

**时点**：LoCoMo 已登记、**formal 一次未跑**、dry 亦未跑。本次修订发生在任何
成绩产生之前；连带影响是用例总数 931→933（新增两条守卫），两份 README 的
五个数字已按守卫自己的推导函数重测并同步。


### 修订 4（v20.0 开发期，formal 跑分前，dry 已试跑但结果作废）

**改了什么**（两处）：
1. 新增 §2.1「灌注消息的 role 契约」：说话人名不再当 role 发出，归一为
   `user` 并把名字前缀进正文；合法 role 原样透传。同时改
   `benchmarks/adapter.py::add_turn`（两个调用点都过它）。
2. §4.2.1 的灌注吞吐从估值「6～7 轮/分钟」改为实测「4.1 轮/分钟」，
   连带把 `sample[0]` 与全量的排期数字一起改对。

**为什么改（诚实交代）**：这是**修一个会伪造成绩的缺陷**，不是调口径。
LoCoMo 把说话人名放 `speaker` 字段，此前被原样当 role 传给 `/add`；服务端
抽取层只认 `system`/`user`/`assistant` 三个分支，**没有 else、没有告警**，
于是整条消息静默落空。实测后果：灌 78 轮，语义向量库属跑分租户 **0 点**，
而逐字库 78 条齐全、上游抽取与嵌入调用照样各花了 77 与 316 次。
**跑分会产出一个看着合理的数字，但那个数字只反映逐字库，与 v20 的语义记忆
能力无关。** 这正是「空答案制造假绿灯」的同族问题：管线不报错，成绩却是假的。

**为什么这不是放水**：
1. 归一**只放宽 role 的写法，不放宽任何评分口径**——温度、token 上限、
   prompt 模板、seed、top-k、数据指纹一个字未动。
2. 合法 role **原样透传**，G3b 的字节形态不受影响；这一条由两条负向对照
   用例钉死（`user` 与 `assistant` 各一条，断言正文不许加前缀）。
3. 归一是**让被测系统能真的收到数据**，不是替它答题。修完仍走生产同路
   `infer=true`，服务端行为未被适配器改动。
4. 修法本身在落地前先做过实证：探针 `role=user` + 名字前缀 → Δ=+1，
   且抽出的事实保留了人名归属；另外两条候选修法（名字当 role、
   映射成 assistant）实测均 Δ=0，**被证伪后才排除，不是凭直觉排除**。

**同期发现的真 bug／真问题**（两个独立缺陷，本次只修了跑分侧那个）：
1. **跑分侧**：`run.py:245`、`:346` 把说话人名当 role 传下去——已修（本次）。
2. **产品侧**：`/add` 收到白名单外的 role 时**静默丢弃整条消息，回执仍是
   `status: ok, action: new`，日志零告警**。这是独立于跑分的产品缺陷
   （静默丢数据且回成功），不管跑分怎么改都该修。**本次未修**，已单独
   记录在 `FINDING_role_drop.md`，留待排期。

**顺手排除的一个假警**：服务端启动告警称未配 `AIDUMEM_ENTITY_KEYWORDS` 会让
「涉及自定义人名的查询被闸门判为 no_signal 并静默零召回」。现场实测三条
人名查询**全部有召回**（分数 0.42～0.66），故该告警**不是** LoCoMo 的第二个
阻塞点。记此以免后人重查。

**遗留的已知局限（如实登记）**：
1. **2026-08-23 之前所有 dry 结果一律作废，不得引用**——它们只测了逐字库。
2. 归一把两个说话人都记成 `user`，抽出的事实会以 `User's name is Caroline …`
   这类形式出现。归属靠正文里的人名保住，**不是靠 role 区分**；这是当前
   抽取层能力下的取舍，已由「两个说话人仍可区分」那条用例守着。
3. 产品侧的静默丢弃缺陷未修（见上）。

**批准**：项目负责人在看过四组对照实验与两个候选修法的证伪结果后，
从 A／B／C 三个选项中明确选定 A（按验证过的修法改并重跑 dry）。

**时点**：LoCoMo 已登记、**formal 一次未跑**；dry 已试跑但因本缺陷作废、
未产出任何 summary。本次修订发生在任何**有效**成绩产生之前；连带影响是
`tests/test_v20_benchmarks.py` 用例数 +4（两条归一 + 两条透传负向对照），
全量 `test_v20_answerer.py` + `test_v20_benchmarks.py` 107→111 全绿。

### 修订 5（v20.0 开发期，formal 跑分前，LongMemEval 数据已登记）

**改了什么**（六处，全是数据来源与状态，评分口径一个字未动）：
1. §1 表格 LongMemEval 行：拆成**代码提交号**（GitHub `9e0b455f…`）与
   **数据修订号**（HuggingFace `2ec2a557…`）两个字段。
2. §1 哈希状态：「尚未取数、尚未登记」→ 已登记事实（headline
   `longmemeval_s.json`、`sha256` 前 16 位 `08d8dad4be43ee20`、278025796 字节）。
3. §1 新增：oracle 的哈希**钉在协议、不进 manifest**，连原因一起写明。
4. §5 的 manifest 说明、§9 遗留局限、`RESULTS.md` 的「数据哈希锁定」状态行
   三处同步改对。
5. `benchmarks/data_manifest.json` 新增 `longmemeval` 条目（LoCoMo 条目未动）。
6. `benchmarks/schemas.py` 的 `haystack_dates` 有序性断言下调粒度（见下）。

**为什么区分两个提交号（不是多此一举）**：字节是从 HuggingFace 数据仓取的，
GitHub 那个提交号是**代码**仓的。拿代码提交号去标注数据字节，等于谎报来源——
第三方按那个号去 GitHub 找不到这些文件，也就无从判断「是取错了版本」还是
「文件被人改过」，而这正是 `download.py` 当初要求登记提交号的全部理由。

**oracle 为什么不进 manifest**：`register()` 每个数据集只写一条
`manifest[dataset]`，而 `_check_formal_manifest` 拿 `--data-path` 的实测哈希跟
这一条对撞。headline 归 `longmemeval_s.json`（§3 明写「oracle 永不作 headline」），
oracle 若也塞进去只会覆盖 s、并让 formal 闸门认错文件。故钉在 §1。

**顺手修了一个我自己造的硬阻塞（诚实交代）**：`schemas.py` 原先要求
`haystack_dates` 按**完整时间戳**升序，两个官方文件**全被拒**（oracle 卡在
`instance[0]`、s 卡在 `instance[233]`），注册根本跑不起来。没有直接判数据有问题，
先量：按完整时间戳乱序 oracle 34/500、s 211/500；而**按天乱序两个文件都是
0/500**，且全部乱序**只是日内时刻颠倒**（34==34、211==211）。结论是上游只按
**天**排序，日内时刻本就无序——**是我的校验器凭空发明了一条上游从未承诺的约束**。
已把结构性断言下调到天粒度，日内颠倒改为计入 report 的 `intraday_unordered`
（本模块既定分工：结构性违规抛异常，数据可疑点进 report 交给 runner）。

**为什么这不是放水**：防时间泄漏的 `sessions_after_question` **仍用完整时间戳**
跟 `question_date` 比，一个字未动（实测 oracle 65、s 1550，runner 必须排除）；
放宽的只是「同一天内先后」这一条上游没做的承诺。跨天乱序仍抛，由负向对照用例
`test_longmemeval_still_rejects_cross_day_unordered_haystack` 钉死——改的是粒度，
不是把守卫删了。改前先量爆炸半径：全仓 `grep 升序` 只 1 处命中（就是那句抛错），
无其他调用方、无用例覆盖。

**上游自证的字节校验（比自己算哈希更强）**：HuggingFace 对 LFS 文件返回的
`x-linked-etag` 就是内容 sha256。两个文件的本地实测值与上游返回值**逐字节相符**
（s `08d8dad4…7894`、oracle `821a2034…620c`），等于上游替我们背书「取到的就是
它发出的字节」，而不只是「我这次跑的是这个文件」。另有一条独立旁证：oracle 与 s
的 `type_counts` 六类**完全一致**、`abstention` 均为 30——oracle 本就是同一批 500
题只留证据会话，数字对得上说明两份文件同源且完整。

**撤回一次我自己下错的「更正」（这一条对我不利，照写）**：我曾在别处判定
修订 4 里「`/add` 收到白名单外 role 时日志零告警」是虚报，理由是上游
`mem0/memory/main.py:888` 有 `logger.warning`。**那个判定是错的，修订 4 是对的。**
逐字复核两处后确认：`main.py:888` 的告警只针对**缺 role 或缺 content** 的畸形
消息，`:891` 只跳 `role=="system"`；而真正吞掉数据的是
`mem0/memory/utils.py:70-75` 的 `if system / elif user / elif assistant`——
**确实没有 else**。说话人名当 role 时，消息在 `main.py` 侧校验全过（role、
content 都在，也不是 system）、逐字库照存，随后在 `parse_messages()` 静默落空，
**全程零告警**。

我错在：看见 role 处理附近有一个 `logger.warning` 就认定「它会告警」，没核对那句
告警管的是哪个条件。这是把**存在性命中**当成了**因果证明**——与「单一存在性探针
会假阴性」同族，只是方向反了，成了假阳性。教训：`FINDING_role_drop.md` 无需撤回，
它当初的定位是对的；该产品侧缺陷仍然**真实、未修、待排期**。

**时点**：LongMemEval 已登记、**formal 一次未跑**。本次修订发生在任何 LongMemEval
成绩产生之前。

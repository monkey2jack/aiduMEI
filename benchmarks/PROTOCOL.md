# aiduMEI 评测协议（v20.0，冻结版）

> 本文件是评测的**先验承诺**：数据、模型、judge、prompt、seed、哈希在跑分
> 之前锁定。任何改动都必须改这份文件并留下版本记录——没有"跑完再定规则"。

## 1. 数据集与许可证

| 数据集 | 官方来源 | 许可证 | 规模 |
|---|---|---|---|
| LongMemEval | github.com/xiaowu0162/LongMemEval | MIT | 每文件 500 实例（`longmemeval_s` / `longmemeval_m` / `oracle`） |
| LoCoMo | github.com/snap-research/locomo | **CC BY-NC 4.0**（仅限非商业评测） | 10 段长对话，QA 总数由装载器现场统计 |

- 数据文件**不进仓库**；存放于 `AIDUMEI_BENCH_DATA_DIR`（默认 `~/.aidumem/bench_data`）。
- `benchmarks/data_manifest.json` 进仓库，记录每个数据文件的 SHA-256。
- **哈希状态：PENDING**——数据尚未登记。`run.py --formal` 在 manifest
  缺失或含 PENDING 时**拒绝启动**，这是代码强制的闸门，不是口头约定。
- LoCoMo 图片只有外部 URL：本管线不抓取、不缓存、不评测任何图片内容。
- 上游数据的已知标注问题（如部分 cat5 只有 `adversarial_answer`、evidence
  引用缺失）由 schema 校验器**如实上报**进 manifest 的 `schema_report.anomalies`；
  原始数据一个字节不改。若评分需要修正，走版本化 correction manifest 并做
  敏感性分析（含/不含修正各报一遍）。

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
- LoCoMo：官方评分脚本；cat5（对抗/拒答）按官方口径，
  只有 `adversarial_answer` 的题以拒答判定。
- 答案模型、prompt 模板、seed：**PENDING**——在第一次正式运行前于此处
  锁定并连同哈希一起提交。锁定前 formal 不得开跑。

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

# 数据登记（手工获取后锁哈希）
.venv/bin/python -m benchmarks.download --register longmemeval longmemeval_s.json
.venv/bin/python -m benchmarks.download --show

# formal（哈希闸门通过后才会启动；--deterministic 在此被硬拒）
.venv/bin/python -m benchmarks.run --formal --dataset longmemeval \
  --data-path "$AIDUMEI_BENCH_DATA_DIR/longmemeval_s.json" --base-url http://127.0.0.1:8100
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

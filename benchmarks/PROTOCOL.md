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

- JSONL 原始记录：逐实例的注入会话、排除会话、检索结果、证据命中、request 计数。
- summary JSON：配置全量（数据哈希、top-k、fixture/data 指纹）、失败分类
  统计（adapter.stats）、确定性 **digest**。
- digest 对确定性内容取 SHA-256，剔除时间戳/延迟/request_id 等波动字段：
  **同一配置跑两遍，digest 必须一致**（G3 复现性闸门）。

## 6. 重试语义的已知取舍

`/add` 非幂等。适配器对 5xx/超时做有限重试（默认 2 次），极端时序下可能
造成重复写入。评测语境下重复记忆只会让检索**更难**而非更容易，不会虚增
成绩，故接受这一偏保守取舍并在此如实记录。`/search`、`/health`、job 轮询
天然幂等，重试无副作用。

## 7. 复现命令

```bash
# smoke（合成 fixture，无需真实数据；两次运行 digest 必须一致）
.venv/bin/python -m benchmarks.run --smoke --dataset longmemeval --base-url http://127.0.0.1:8100
.venv/bin/python -m benchmarks.run --smoke --dataset locomo     --base-url http://127.0.0.1:8100

# 数据登记（手工获取后锁哈希）
.venv/bin/python -m benchmarks.download --register longmemeval longmemeval_s.json
.venv/bin/python -m benchmarks.download --show

# formal（哈希闸门通过后才会启动）
.venv/bin/python -m benchmarks.run --formal --dataset longmemeval \
  --data-path "$AIDUMEI_BENCH_DATA_DIR/longmemeval_s.json" --base-url http://127.0.0.1:8100
```

# 决策模型选择：一次真实 HTTP 与 CLI 样本测试

这份记录是 aiduMEI f0.3++ 的公开彩蛋。我们在以下环境和条件中更倾向 **Jev 1.13.0**；需要更低延迟时可考虑 **Drex 1.5**。**我们仅对本次测试样本、环境和条件下的观测负责。** 这不是通用模型排名、服务承诺或未来版本的性能保证，模型仍由使用者选择。

## 测试环境与条件

| 条件 | 本次设置 |
|---|---|
| 日期与源码 | 2026-10-05；候选 `dff08f3a48875d5fcca9c5e711406737e58b6d0f`；包版本 `0.3.0+decision.1`。这是历史测量，未在最终 f0.3++ 提交上重跑五组评估。 |
| 执行环境 | Linux 生产主机上的隔离实例、Python 3.12.3、SQLite 与独立 Qdrant 服务、真实已安装 Hermes CLI 和读钩子。环境描述不包含真实用户数据、地址或凭据。 |
| 固定模型链 | 相同主模型与人设；抽取链 `step-3.7-flash`，嵌入 `BAAI/bge-m3`，重排 `BAAI/bge-reranker-v2-m3`。完整宿主配置和供应商路由未公开，因此本文不能独立复现全部端到端环境。 |
| 产品参数 | 支持阈值 0.6，分类采纳阈值 0.7，决策单次超时 2000 ms；各组其余产品设置相同。 |
| 数据与隔离 | 24 个独立冻结合成家族：16 个有答案、8 个无答案；2 个原话题属于有答案子集。资料经真实接口逐条回读并核哈希；每组独立租户，决策缓存从冷状态开始，按家族轮换调用。 |
| 两条测量轴 | 每组 24 次 HTTP 检索、24 次真实 CLI 最终回答；五组共 120 次 HTTP + 120 次 CLI。两条轴评价同一批 24 个家族，**不是 240 个独立样本**。 |
| CLI 约束 | 保留真实读钩子、主模型和人设；关闭写钩子、插件及原生写工具，避免评估生成新证据。 |
| 判定与失败 | 两名匿名模型评审员先分别封存判断，再揭示组别与耗时；不是人工评审。等义短答可通过，对象、时间、状态和值须正确；无答案检索须为空且 `not_found`，最终拒答独立计分。执行失败留在原分母，不择优重试。 |

## 结果

每行五个决策配置之外的设置相同。检索指标衡量返回证据，CLI 指标衡量主模型最终回答，二者不能互相替代。

| 配置 | HTTP 通过 /24 | known 召回 /16 | unknown 空且 not_found /8 | CLI 回答 /24 | unknown 最终拒答 /8 | 原话最终引用 /2 | HTTP p50/p95 ms | CLI p50/p95 秒 |
|---|---:|---:|---:|---:|---:|---:|---|---|
| 关闭决策 | 17 | 16 | 1 | 23 | 8 | 1 | 653 / 1092 | 16.626 / 30.443 |
| Drex 1.5 | 21 | 15 | 6 | 22 | 8 | 1 | 1243 / 1654 | 17.176 / 21.606 |
| Jev 1.13.0 | 21 | 16 | 5 | 23 | 8 | 1 | 1476 / 3663 | 16.344 / 22.604 |
| Clef | 21 | 16 | 5 | 23 | 8 | 1 | 2115 / 4187 | 18.092 / 22.393 |
| Clef Flash | 22 | 16 | 6 | 23 | 8 | 1 | 1601 / 4531 | 16.851 / 26.804 |

所有组 HTTP 执行失败、CLI/钩子执行失败均为 0；执行成功不代表答案正确。关闭决策的最终回答也为 23/24，因为主模型会自行拒答。决策模型在本样本中的直接收益是检索卫生：无答案问题返回空且 not_found 从 1/8 增至 5–6/8。

分类诊断另复用 24 条既有诊断文本，不能当作新增独立样本。Jev 采纳决策 16/24、回退主模型 3/24；Drex 为 14/24、5/24；Clef 为 2/24、14/24；Clef Flash 为 3/24、13/24。HTTP 决策阶段 `error_fallback` 则分别为 Drex 0/37、Jev 1/35、Clef 1/35、Clef Flash 0/36：Jev 的“较少回退”指分类诊断，不是所有测量轴都最少。

## 为什么这样推荐

事前选择顺序是最终回答、无答案拒答和原话引用；质量接近时，再看无效证据接受、决策错误及回退，最后看尾延迟。Jev 在最终回答上并列最好、已知召回完整，分类诊断比 Clef 系更常采纳决策，HTTP p95 在 Jev/Clef/Clef Flash 三者中最低。因此它是本环境的条件性推荐。Clef Flash 的无答案检索多通过一个家族，但尾延迟较高。Drex 最快，却误滤一个已知事实并影响最终回答；这只是一次样本损失，不是对未来召回风险的估计。

五组都在 p16 失败：887 字记录的关键句位于第 765 字，HTTP 已找到完整记录，但关键句超出读钩子的注入预算。这个结果保留，不能归咎于某个决策模型，也不能宣称最终版本已修复此限制。

每格仅运行一次，样本小、包含模型随机性；没有价格比较、独立留出集校准或统计显著性结论。网络、服务端版本、主模型、文本长度、重排及预算变化都可能改变结果。请用自己的数据验证后再决定。

## English

Our conditional preference is **Jev 1.13.0**, with **Drex 1.5** as a lower-latency option. **We stand behind these observations only for the tested samples, environment and conditions.** They are not a universal ranking, SLA or performance guarantee for the final release.

This historical run used the 2026-10-05 candidate `dff08f3a48875d5fcca9c5e711406737e58b6d0f` (`0.3.0+decision.1`), a Linux host, Python 3.12.3, isolated API instances, SQLite/Qdrant and the installed Hermes CLI/read hook. The extraction, embedding and reranking models were `step-3.7-flash`, `BAAI/bge-m3` and `BAAI/bge-reranker-v2-m3`. The main model/persona and other settings stayed fixed; full host configuration and provider routing are not public, limiting independent reproduction. Support/classification thresholds were 0.6/0.7 and decision timeout was 2000 ms. The five-arm evaluation was not rerun on the final f0.3++ commit.

Each arm scored the same 24 frozen synthetic families (16 answerable, 8 unanswerable, including 2 quote questions within the answerable subset), using separate tenants, cold decision caches, verified source records and rotated call order. There were 120 HTTP retrievals plus 120 CLI answers, **not 240 independent samples**. Write hooks, plugins and native write tools were disabled. Two anonymous model graders, not humans, sealed judgments before labels/timing were revealed; failures remained in the denominator. The table above separates retrieval from final answers and reports milliseconds for HTTP and seconds for CLI.

Jev tied the best final answers at 23/24, retained all known facts, adopted more classification decisions than the Clef models, and had lower HTTP tail latency among Jev/Clef/Clef Flash. It did not have the fewest fallbacks on every axis: HTTP-stage fallbacks were 1/35 for Jev and Clef, 0/37 for Drex and 0/36 for Clef Flash. Drex was faster but over-filtered one known family. Decision-off also scored 23/24 final answers; the measurable improvement was cleaner evidence for unknowns (empty+not_found increased from 1/8 to 5–6/8).

All arms failed p16 because the key sentence exceeded the read-hook injection budget. One run per cell, model randomness, no pricing study, no independent holdout calibration, no significance claim; the user chooses the model and should validate their own workload.

## 合成案例矩阵 / Synthetic case matrix

每格两个符号：第一个是检索（无答案题为“空且 not_found”），第二个是最终回答（无答案题为“明确拒答且无编造”）。✓ 通过，✗ 未通过。全部为合成任务。

| case | 抽象任务 | 类型 | 关闭决策 | Drex 1.5 | Jev 1.13.0 | Clef | Clef Flash |
|---|---|---|---|---|---|---|---|
| p01 | 别名与农历日期的等义提问 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p02 | 按指定时间选择更新后的事实 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p03 | 明确否定的事实 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p04 | 同一属性的对象与层级约束 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p05 | 相对比较不能推出绝对数值 | unknown | ✗✓ | ✗✓ | ✗✓ | ✗✓ | ✗✓ |
| p06 | 数值事实与简短等义回答 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p07 | 小数与单位 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p08 | 区分提议与已批准决策 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p09 | 仅有提议时承认未定 | unknown | ✗✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p10 | 精确联系字段 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p11 | 代码字面量 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p12 | 操作先后顺序 | known | ✓✓ | ✗✗ | ✓✓ | ✓✓ | ✓✓ |
| p13 | 决策中的保留与回滚约束 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p14 | 代词指代与对象约束 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p15 | 短原话的逐字引用 | known（原话子集） | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p16 | 长混合原话中的末尾关键语句 | known（原话子集） | ✓✗ | ✓✗ | ✓✗ | ✓✗ | ✓✗ |
| p17 | 指定时间的版本更新 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p18 | 偏好的等义提问 | known | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p19 | 未记录的唯一标识应拒答 | unknown | ✗✓ | ✗✓ | ✗✓ | ✗✓ | ✗✓ |
| p20 | 未实测的数值应拒答 | unknown | ✗✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p21 | 提及记录但没有所问值 | unknown | ✓✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p22 | 有状态记录但没有原因 | unknown | ✗✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p23 | 有事件记录但没有责任人 | unknown | ✗✓ | ✓✓ | ✓✓ | ✓✓ | ✓✓ |
| p24 | 相对人数不能推出准确人数 | unknown | ✗✓ | ✓✓ | ✗✓ | ✗✓ | ✓✓ |

**读矩阵的要点**：
- p16（长混合原话）五组一致失败：检索拿到完整记录，但读钩子只注入了截断片段，关键句丢失（见第 8 节）。
- p09、p20、p22、p23：只有关闭决策返回了无关证据，四个决策模型都过滤成功（p20、p22、p23 同此）。
- p12：只有 Drex 丢失——它把该已知事实的证据过滤成“空且 not_found”，检索与回答都没了。
- p05、p19：五组（含全部决策模型）都对无答案问题返回了相关但不能回答的证据；最终回答仍正确拒答。
- p24：Jev 与 Clef 仍返回了相关证据，Drex 与 Clef Flash 过滤成功；各组最终回答都正确拒答。

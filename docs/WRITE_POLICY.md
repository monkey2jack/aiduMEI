# 什么值得记（f0.7+ 写入判定）

aiduMEI 每轮都存原话。f0.7+ 在写入时给每一条记忆做一次判定，按九条原则打标签、记日志；
其中**原则 7（永远不记密钥/证件/签名/原文拷贝）直接生效**；f0.8 起按影子周结果又开了三处拦截（原则 1 的两类机器噪声、
原则 8 的忘记规则、会话精华里的模型思考过程，见下节），其余仍是**影子模式**：只判定、只记日志，不拦、不改。

| # | 原则 | 落点 | 默认 |
|---|---|---|---|
| 1 | 只记长期有用的 | 写入判定 `verdict = keep / daily / reject`（宿主机器话、测试口令 → reject；短确认、单字、「批准，做起来」这类拍板确认 → daily，不丢）。f0.8：机器文本（`[SILENT]`、图片占位）与上下文续接说明**拦下进墓碑** | 两类机器噪声**拦截**；其余影子 |
| 2 | 带条件记 | 元数据 `condition`（occasion / place / people）；会话精华提示词禁止「一次→习惯」「通常→总是」 | 只加字段 |
| 3 | 分清说的和猜的 | 元数据 `source_type = direct / inferred` + `basis`；`/search` 每条带 `inferred`，CC/Codex 读线钩子给推断条目加「（推断）」 | 生效（只标注） |
| 4 | 改现状，不堆流水 | 同话题新说法 → 日志记「本应替换」哪条旧记忆（id）。f0.8：先要求话题重合（实词重合或向量相近），只共享「是/不是」「既然」这类判断词不算 | 影子 |
| 5 | 冲突听新的、听本人的 | 本人直说 > 推断；新 > 旧；推断要推翻本人说过的、或同一秒 → `pending_confirm` | 影子 |
| 6 | 三层分开 | 元数据 `layer = profile / topic / daily`；`AIDUMEI_RECALL_EXCLUDE_DAILY=1` 时召回不带 daily | 打标签；过滤默认关 |
| 7 | 永远不记 | 密钥、令牌、私钥、证件号、卡号、签名图：只把那一段换成 `[redacted:类型]`；整段邮件原文 / 超长原文拷贝 / 只有一把钥匙的写入：整条 422 拒收 | **直接生效** |
| 8 | 说忘就彻底忘 | `POST /api/forget`：按 id 走与 `/delete` 同一条级联（向量、全文、事实、原文、派生精华、判定日志）；`never_store` 规则写入时比对，命中的那条不落任何一层（判定日志只留规则 id，不留哈希） | 删除生效；规则**拦截**（f0.8） |
| 9 | 召回少而准 | `AIDUMEI_RECALL_PRECISE=1`：最多 K 条、分数不到最高分的一半的不要 | 默认关 |

## f0.8 开了哪些拦截

| 拦什么 | 怎么拦 | 拨回影子 |
|---|---|---|
| 原则 1：机器文本（`[SILENT]`、`[Image: original …]` 等）、上下文续接说明（`This session is being continued…`） | `/add` 在任何一层落库前拿出来，进墓碑（与删除同一张表），`AIDUMEI_WRITE_POLICY_GATE_TTL_DAYS`（默认 7）天内可用 `/tombstone/restore` 恢复，到期擦正文；同一轮里的其他消息照常写 | `AIDUMEI_WRITE_POLICY_P1=shadow` |
| 原则 8：命中「不再保存」规则的写入 | 整条不写，也不进墓碑 | `AIDUMEI_WRITE_POLICY_P8=shadow` |
| 会话精华里的模型思考过程（「We need to output…」、`<think>…</think>`、harmony 分析通道） | `/session/distill` 先剥包装，剩下的仍像思考就走确定性降级摘要（metadata `distill_fallback_reason`）；`/add` 收到 LLM 模式精华仍是思考文本时进墓碑。降级摘要（引用本人原话）不判 | `AIDUMEI_WRITE_POLICY_THINKING=shadow` |

`/add` 回执里的 `gated` 列出拦下的条目（原则、理由码、墓碑 id），不含正文；判定日志里这些行 `mode=enforce`。
同一道拦截（`write_policy.enforce_messages`）也挂在其他写路径：`/update` 的新正文（先验归属，被拦则原记忆不动，回执 `action=gated`）、后台 `auto_memory` 环（逐条拦后再总结，整段再比一次忘记规则）；MCP `mem_add` 与宿主 `run_auto_memory` 本就走 `/add`，`/session/distill` 只提炼不落库；墓碑写失败时照旧写入。
短确认与单字（「好，那走吧」「改」）**不拦**，判 daily。`AIDUMEI_WRITE_POLICY=off` 时以上全部不生效（原则 7 不受影响）。
原则 4/5 的话题重合门槛：`AIDUMEI_WP_TOPIC_MIN_OVERLAP`（默认 0.3）、`AIDUMEI_WP_TOPIC_MIN_COSINE`（默认 0.8）。

## 判定日志

`<DATA_DIR>/write_policy.db`（0600）：

- `verdicts`：每条写入一行——时间、租户、记忆 id、正文 sha256 前 32 位、长度、判定、层级、出处、理由码、条件、被屏蔽的类型、整条拒收原因、命中的忘记规则、本应替换的旧记忆 id。**不存正文**。记忆删除时按 id 一并删掉。
- `forget_rules`：「不再保存此类」规则。
- `key_events`：钥匙被用在它没绑定的身份上的计数（见下）。

查询：`GET /api/write-policy/verdicts?user_id=…&since=…&verdict=reject`（`summary=1` 给计数）。

误判复盘：`python scripts/write_policy_replay.py --data-dir <数据目录副本> --user <id> --samples <文件>`
对存量数据重放一遍判定（只读打开），`--samples` 文件含短预览，只供人工复核，不要提交。

## 忘记

```
POST /api/forget {"user_id": "...", "query": "旧地址"}                    → 只列候选，不删
POST /api/forget {"user_id": "...", "memory_ids": [...], "never_store": "旧地址"}  → 删 + 记规则
GET  /api/forget/rules?user_id=...
POST /api/forget/rules/delete {"user_id": "...", "rule_id": 1}
```

按模糊查询直接删会误伤别的记忆，所以查询只列候选，删除必须给 id。删除进墓碑，`AIDUMEI_TOMBSTONE_TTL_DAYS` 内可恢复，到期擦正文。

## 每个 agent 一把钥匙

见 `.env.example` 的 f0.7+ 段与 `scripts/agent_keys.py`。服务端只存钥匙的 sha256；
默认影子（`AIDUMEI_AGENT_KEY_ENFORCE=0`）只给「身份对不上」记账，所有 agent 都换上自己的钥匙后再设 1。

f0.7.3 起：
- 钥匙身份在**每一条**已分类路由上都核，不只是调用了跨殿授权的那几条：读 `/stats`、`/recent`、`/tombstones`、`/events/history`、`/write-policy/verdicts`、`/dossier`，删 `/delete`、`/forget` 等都要求目标身份在钥匙的绑定里；`user_id` 为空（老代码里意为「全部租户」）按全实例处理。
- agent 钥匙（含绑了身份的主钥匙）不做全实例操作（`/metrics`、`/usage`、`/config` 等），这些留给控制台登录（主人）。
- 钩子按身份读钥匙文件 `<AIDUMEI_KEYS_DIR>/<user_id>.key`（默认 `~/.aidumei-keys`），运行时读文件，不上命令行；没有就用 `AIDUMEM_API_TOKEN`。
- `delete_all` 只清本域判定日志，**保留**「不再保存」规则（那是对以后的指令）。

## 召回开关的实测（真实回归集 v3，六类，离线）

| 配置 | 总分 | 结论 |
|---|---|---|
| 默认（两个开关都关） | 与 f0.7.1 逐类一致 | — |
| `AIDUMEI_RECALL_PRECISE=1`（K=5，比例 0.5） | 逐类一致 | 可开，但当前读线只取 3 条，收益小 |
| `AIDUMEI_RECALL_PRECISE=1`，比例 0.7 | fact_recall 下降 | 不开 |
| `AIDUMEI_RECALL_EXCLUDE_DAILY=1` | 大幅下降 | 不开：层级判定把大量运维过程归为 daily，先看影子数据 |

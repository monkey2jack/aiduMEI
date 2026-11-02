# 什么值得记（f0.7+ 写入判定）

aiduMEI 每轮都存原话。f0.7+ 在写入时给每一条记忆做一次判定，按九条原则打标签、记日志；
其中**原则 7（永远不记密钥/证件/签名/原文拷贝）直接生效**，其余默认**影子模式**：只判定、只记日志，不拦、不改。
观察一段时间、确认不误杀之后，再逐条打开。

| # | 原则 | 落点 | 默认 |
|---|---|---|---|
| 1 | 只记长期有用的 | 写入判定 `verdict = keep / daily / reject`（寒暄、宿主机器话、测试口令 → reject；「批准，做起来」这类拍板确认 → daily，不丢） | 影子 |
| 2 | 带条件记 | 元数据 `condition`（occasion / place / people）；会话精华提示词禁止「一次→习惯」「通常→总是」 | 只加字段 |
| 3 | 分清说的和猜的 | 元数据 `source_type = direct / inferred` + `basis`；`/search` 每条带 `inferred`，CC/Codex 读线钩子给推断条目加「（推断）」 | 生效（只标注） |
| 4 | 改现状，不堆流水 | 同话题新说法 → 日志记「本应替换」哪条旧记忆（id） | 影子 |
| 5 | 冲突听新的、听本人的 | 本人直说 > 推断；新 > 旧；推断要推翻本人说过的、或同一秒 → `pending_confirm` | 影子 |
| 6 | 三层分开 | 元数据 `layer = profile / topic / daily`；`AIDUMEI_RECALL_EXCLUDE_DAILY=1` 时召回不带 daily | 打标签；过滤默认关 |
| 7 | 永远不记 | 密钥、令牌、私钥、证件号、卡号、签名图：只把那一段换成 `[redacted:类型]`；整段邮件原文 / 超长原文拷贝 / 只有一把钥匙的写入：整条 422 拒收 | **直接生效** |
| 8 | 说忘就彻底忘 | `POST /api/forget`：按 id 走与 `/delete` 同一条级联（向量、全文、事实、原文、派生精华、判定日志）；`never_store` 规则写入时比对 | 删除生效；规则影子 |
| 9 | 召回少而准 | `AIDUMEI_RECALL_PRECISE=1`：最多 K 条、分数不到最高分的一半的不要 | 默认关 |

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

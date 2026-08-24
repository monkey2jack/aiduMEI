# 缺陷记录：说话人名当 role → 语义记忆层零入库

**发现时间**：2026-08-23  **发现场景**：v20.0 LoCoMo dry 跑分（沙箱）
**状态**：已定位、已验证修法；**灌注口径属 LOCKED，改法待裁**

## 1. 现象

dry 跑分灌注 78 轮后，语义向量库（Qdrant `aidu_mem`）中属于跑分租户的
点数为 **0**。逐字库（Verbatim Vault）却完全健康：78 条原文在库、内容正确。

上游是真花了钱的：StepFun 抽取调用 77 次、硅基嵌入调用 316 次。
**服务端零告警**——`/add` 一律回 `status: ok, action: new`。

即：跑分只在量逐字库那一条路，整个语义记忆层贡献为零。
分数会「跑得出来」且看着合理，但它**不是**被测系统的成绩。这是假绿灯。

## 2. 定位过程（三组对照 + 一组补充）

已排除：配置（collection/path/1024 维均正确）、第二个 Qdrant、仓库污染、
任务队列、写入通路本身、`bank_id`、内容形态。

| 试验 | role | content | Qdrant Δ |
|---|---|---|---|
| A | `user` | LoCoMo 原句 | **+1** |
| B | `Caroline` | 同一句 | **0** |
| C | `Caroline` | 第一人称明示事实 | **0** |
| D | `assistant` | 第一人称明示事实 | **0** |

A vs B 把变量锁死在 role 上；C 排除「内容不像事实」这个解释；
D 排除「原生两方对话映射」这条修法。

**结论：只有 `role: "user"` 能产出语义记忆。**

## 3. 根因（两个独立缺陷）

**缺陷 A（跑分侧）**：`benchmarks/run.py:245` 与 `:346` 把 LoCoMo 的说话人名
（`Melanie`/`Caroline`）直接当 `role` 传下去，违反 OpenAI 的 role 契约。

**缺陷 B（服务/库侧，与跑分无关的产品缺陷）**：
`mem0/memory/utils.py:61 parse_messages()` 只认 `system`/`user`/`assistant`
三个分支，**没有 else、没有告警**：

```python
for msg in messages:
    if   role == "system":    response += ...
    elif role == "user":      response += ...
    elif role == "assistant": response += ...
    # 没有 else —— 其余 role 静默落空
```

role 不在白名单 → 抽取提示词是空串 → 抽不出事实 → 零点入库，
而 `/add` 照样回 `ok`。**静默丢数据且回成功**，这一条本身就该修，
不管跑分怎么改。

（`assistant` 在白名单内却仍 Δ=0，说明抽取只挖 user 侧事实。
本记录不为 assistant 分支的机制下断言，只记实测结果。）

## 4. 已验证的修法

`probe-fix-005`：`role` 归一为 `"user"`，说话人名并入 content
→ **Δ=+1**，且抽出的事实保留了归属：

> `User's name is Caroline and she is researching adoption agencies ...`

落点应在 `benchmarks/adapter.py:add_turn`（两处调用都过这里），
`role` 形参保留说话人名以供 metadata / 逐字库存真，
发往 `/add` 的 payload 里归一为 `user` 并把说话人名前缀进 content。

## 5. 口径影响（重要）

这是对 **LOCKED 灌注口径**的改动，发生在验证进行中，
按项目规矩属关键决策，**须先获批准**再落地与重跑。

同时：2026-08-23 之前所有 dry 结果一律作废，不得引用——
它们只反映逐字库。

## 6. 顺手排除的一个假警

服务端启动告警称未配 `AIDUMEM_ENTITY_KEYWORDS` 会让「涉及自定义人名的查询
被闸门判为 no_signal 并静默零召回」。现场实测三条人名查询**全部有召回**
（分数 0.42–0.66），故此告警**不是** LoCoMo 的第二个阻塞点。记此以免后人重查。

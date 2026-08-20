# ADR-001 · 向量后端契约与 sqlite-vec 影子 POC（v20.0）

- **状态**：已采纳（v20.0）
- **决定人**：v20.0 发布链路
- **关联**：`ducky/vector_backend.py`（契约与双后端适配器）、
  `scripts/vector_shadow_poc.py`（影子迁移与校验脚本）、
  `tests/test_v20_vector_backend_contract.py` / `tests/test_v20_vector_migration_poc.py`

## 背景

aiduMEI 的向量检索自 v8 起绑定 mem0 + 本地 Qdrant。v20.0 计划（P0-5）要求：
在**不改变默认生产数据**的前提下，把「后端」从隐式依赖升级为显式契约，
并实测 sqlite-vec 作为轻量备选的能力、平台、性能与许可——为将来可能的
换库决策准备证据，而不是准备承诺。

## 决定

1. **Qdrant 仍是唯一默认后端。** `AIDUMEM_VECTOR_BACKEND` 缺省即 qdrant；
   sqlite-vec 必须显式开启且标记 experimental，**没有任何静默回退**——
   后端选择是运维决策，不是异常处理分支。
2. **后端能力收敛为六个动词的契约**（`VectorBackend` Protocol）：
   upsert / search / delete / count / health / snapshot。两个适配器
   （`QdrantBackend`、`SQLiteVecBackend`）都只通过契约面被使用与被校验。
3. **不可逆的换库与数据域迁移永远不绑在同一次发布里。** v20.0 只交付
   契约、影子工具和实测证据；真要换库，必须另立计划、另过停止点。
4. **影子先行**：任何迁移演练都用 `scripts/vector_shadow_poc.py` 对着
   **快照拷贝**跑——源库被 `ReadOnlyQdrant` 白名单代理锁死（写动词一碰
   就炸），源目录有 `.lock` 直接拒绝开工，检查点与目标不匹配拒绝续跑。
   **回退 = 删掉影子文件**，默认后端与生产数据从头到尾没被碰过。

## sqlite-vec 实测结论（2026-08-21，macOS arm64）

数据来源：`scripts/vector_shadow_poc.py --selftest`（内存 Qdrant + 临时
SQLite，双真后端，非 mock）。环境：Python 3.12.13 / SQLite 3.53.1 /
qdrant-client 1.19.0 / macOS-26.5.2-arm64。

| 维度 | 实测结果 |
|---|---|
| **能力** | 六动词契约全通过；迁移后计数、逐点自检索（分数≈1.0 命中自己）、payload 逐字节、过滤计数全平价；top-k 完全一致率 1.0、Jaccard 1.0、最大分数偏差 0.0（500 与 5000 点两档均如此） |
| **性能** | 检索为 **Python 全表扫描余弦**（health 里如实写 `scoring: python-cosine-fullscan`）：500 点 ≈5.9ms/查，5000 点 ≈63.8ms/查，线性劣化；同库 Qdrant 分别为 0.28ms / 0.60ms。**结论：POC 级正确性基准可用，生产级检索不可用**，除非 sqlite-vec 原生扩展落地 |
| **平台** | 本机未安装 sqlite-vec 扩展，`require_extension=True` 门禁如实拒绝（`extension path is not configured`），且失败**不落任何文件**（体检无副作用，有测试盯着）。`extension_loaded=True` 也不代表检索走了扩展——打分路径单独上报，防止误读为「已加速」 |
| **许可** | sqlite-vec：MIT / Apache-2.0 双许可；qdrant 服务端与 qdrant-client：Apache-2.0。与本仓库开源发布无冲突 |

## 后果

- 好处：后端从「mem0 里长出来的事实」变成可测试、可替换、可观测的契约；
  换库演练有了不碰生产的标准工具；健康面三态（真探活 / 具名降级 / 冷启动
  未探测）杜绝假绿灯。
- 代价：sqlite-vec 路径目前只配得上「实验」二字；契约面是最小集，
  Qdrant 的高级能力（快照 API、ANN 参数）不经契约面暴露。
- 红线重申：`QdrantBackend.snapshot` 故意抛错——Qdrant 快照必须走部署侧
  官方 API，不给「看起来备份了」留活口。
- 已知运维细节：qdrant-client 本地模式**打开库时会自建 `.lock`**（哪怕只读），
  所以同一快照拷贝上的第二次运行（断点续跑）会被 `.lock` 门禁拦下——
  这是快照拷贝，确认无进程持有后删掉 `.lock` 再续即可。门禁宁可多拦一次，
  也不做「猜测这个锁是不是自己人」的聪明事。

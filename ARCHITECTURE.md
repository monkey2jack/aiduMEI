# aiduMEI 架构速览

> 优忆思目录地图。细节见各模块 docstring。

## 神谱

| 版本 | 代号 | 要点 |
|------|------|------|
| v8 | Prometheus | J-space 五脉 |
| v9 | Tahoe-Gate | 闸门 · 零退化 · 情绪半衰 |
| v9.1 | Mnemosyne | 潮浪并忆 · 双策分档 · speed/hot/legacy/salience/extended 子包 |
| v11.1 | Hyperion | 线程本地连接池 · 性能纪元 |
| v12.0 | Chronos | 双时间轴 valid_from/valid_to · 失效降权不删除 |
| **v13.0** | **Pantheon** | 多 Agent 联邦 · MoE 门控 · 分层衰减 · 写入去重 · `federation/` 子包 |
| **v14.0** | **Aegis** | **零硬编码 · 环境变量注入 · 仓库根自解析 · 开箱可部署** |

## 目录

```
<repo-root>/
├── api_server.py              # 入口只组装（~110 行）
├── ARCHITECTURE.md            # 本地图
├── mem0_config_local.json     # 现网配置（含 _speed）
├── mem0_sync.py / health_check.py / mcp_server.py / auto_memory.py
├── ducky/
│   ├── speed/                 # ★ 高速写入（原 add_speed 1269）
│   │   config · cache · fastpath · jobs · stats · coalesce · patch · pipeline
│   ├── hot/                   # ★ HOT 主链路（原 routes_core 605）
│   │   health · add · search · crud
│   ├── legacy/                # ★ 事实层（原 legacy_routes 748）
│   │   helpers · background · routes_facts · routes_observe · routes_scene
│   ├── salience/              # ★ 显著性引擎（原 memory_salience 410）
│   │   config · db · core · metrics · conflict · audit
│   ├── extended/              # ★ 扩展 + 自动记忆（原 routes_extended 266）
│   │   routes · auto_memory
│   ├── federation/            # ★ v13 联邦层（多 Agent / 多 Profile · MoE）
│   │   schema · tier · dedup · registry · recall · router · broadcast
│   │   writer · routes
│   ├── add_speed.py / routes_core.py / legacy_routes.py
│   │   memory_salience.py / routes_extended.py   ← 全部仅为兼容门面
│   ├── routes_v8.py · mem0_runtime.py · text_fts.py
│   ├── memory_gate / workspace / broadcast / jlens / persistence
│   ├── hybrid_recall · recall_funnel · layer1_selfcheck
│   └── utils · tool_envelope · api_models · instinct_graduation
├── scripts/consolidator.py · backfill_tiers.py
├── data/                      # 库 + coalesce_stats.json
├── backups/                   # 改前快照（只留最近）
├── legacy/archive/            # 死脚本归档（不删）
├── tests/ · docs/ · integrations/
└── manifest.json · CHANGELOG.md · CUSTOMIZATIONS.md
```

## 主链路

```
POST /add
  → ducky.hot.add
  → async? → job + (可选) speed.coalesce 缓冲
  → speed.pipeline / layer1 + mem0.add + FTS
  → salience.on_memory_added（显著性注册）
```

## 联邦链路（v13 Pantheon · MoE 门控）

```
GET /federation/recall
  → federation.router.decide       门控：显式参数 > 联邦意图词 > 单Agent > 默认热通道
  → federation.recall.federated_recall
      L1 本 Agent SQL  →  L2 分层加权（tier × decay × trust）
      └─ 不足且联邦开 → L3 同 profile 共享 → L4 跨 profile 全局
  → (可选) rerank：0.6 Jaccard + 0.4 分层得分
  → results + ladder（走到第几级一目了然）

POST /federation/facts/add
  → federation.writer.write_fact
      归属(agent_id/profile/shared) → 分层(显式 or 推断)
      → 去重(≥0.85 merge / ≥0.70 update / else insert)
      → 落库(recorded_at + decay_at；procedural 的 decay_at 恒为 NULL)
```

**分层生命周期**

| tier | TTL | 权重 | 语义 |
|------|-----|------|------|
| `procedural` | 永久 | 1.00 | 铁律 / 规范 / 范式——零衰减 |
| `semantic` | 180 天 | 0.85 | 配置 / 偏好 / 知识 |
| `episodic` | 30 天 | 0.70 | 事件 / 日记 / 会话 |

衰减以 TTL 为半衰期做指数降权，**只沉底不删除**，永不归零。

## 兼容铁律（旧 import 全绿）

| 旧路径 | 真源 |
|--------|------|
| `ducky.add_speed` | `ducky.speed.*` |
| `ducky.routes_core` | `ducky.hot.*` |
| `ducky.legacy_routes` | `ducky.legacy.*` + `text_fts` re-export |
| `ducky.memory_salience` | `ducky.salience.*` |
| `ducky.routes_extended` | `ducky.extended.*` |
| `from api_server import get_memory` | `ducky.mem0_runtime` 再导出 |

## 重构铁律

1. **只搬家，不改语义**
2. **旧 import 路径保留**
3. **一个主题一个改** → `py_compile` → `systemctl restart` → curl 验收
4. **禁止** 8767 已监听时再裸起第二份 api_server
5. **密钥不进 git**（`.sf_key` / `.llm_key`）
6. **联邦层只加不减**：schema 迁移只 `ADD COLUMN`；写入只 insert/update/merge，不删既有行
7. **降级不抛错**：联邦端点任何异常返回结构化 error，绝不让记忆层拖垮上层 Agent

## 运维一键

```bash
systemctl restart aidumem-api.service
curl -s http://127.0.0.1:8767/health
curl -s http://127.0.0.1:8767/add/coalesce/stats

# 联邦层
curl -s http://127.0.0.1:8767/federation/agents
curl -s http://127.0.0.1:8767/federation/tiers
curl -s 'http://127.0.0.1:8767/federation/awareness?agent_id=default'

# 分层回填（dry-run 是默认，--apply 才写）
./venv/bin/python scripts/backfill_tiers.py
./venv/bin/python scripts/backfill_tiers.py --apply

# 联邦层单测（临时库，不碰生产数据）
./venv/bin/python -m pytest tests/test_federation.py -v
```

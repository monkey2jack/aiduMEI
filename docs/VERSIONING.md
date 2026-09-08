# VERSIONING.md — 版本治理政策（v20.4.0-alpha · P2-16）

> **为什么成文**（六方外审 Sonnet A1 实锤）：v14.0.0 → v20.3.0 仅用 31 天，
> 8 月 6 日一天连打 3 个公开 tag、8 月 7 日连打 4 个 —— 版本号不再能作为
> 变更风险的可靠信号。v20.3.4 起已确立双轨纪律，本文把它写成政策。

## 双轨制

| 轨道 | 仓库 | 版本形态 | 说明 |
|---|---|---|---|
| 私有持续线 | `monkey2jack/aiduMEI_dudu`（小仓） | 三级版本 `X.Y.Z` + 阶段后缀 tag（如 `v20.4.0-alpha`） | 每个功能/修复版递增；tag + Release 必须齐备 |
| 公开稳定线 | `monkey2jack/aiduMEI`（大仓） | 两段式 `X.Y` | 只在正式版推进；公开 Tag / Release 一经发布**绝对不可变** |

## 规则

1. **单源生成**：版本号唯一真相源是 `ducky/version.py` 的 `SERVICE_VERSION`；
   `pyproject.toml`、`manifest.json`、`CHANGELOG.md` 首条、`LINEAGE` 首条四处对齐，
   由守卫测试断言（test_v19_3_hardening / test_v19_2_security_and_consistency）。
2. **阶段命名**：大版本开发周期内按希腊字母阶段推进：alpha → beta → gamma（→ 正式）。
   阶段身份体现在小仓 tag 与 Release（如 `v20.4.0-beta`），`SERVICE_VERSION` 保持
   纯数字（v19.3 起守卫拒绝 `20.0-beta` 这类形态进版本号）。
3. **公开克制**：公开仓同日不连打多个 tag；公开维护增量以 commit 推进 main，
   不新打三级版本 tag（v20.3 先例：容器维护进 main，版本与 Release 保持 v20.3）。
4. **不可变**：既有公开 tag 与 Release 永不修改、永不删除；历史问题只能向前修。
5. **谱系**：每个版本在 `ducky/version.py` 的 `LINEAGE` 与 `CHANGELOG.md` 留
   主题与实测数字；预发布阶段的数字以收口回填为准（见 docs/TESTING.md 口径）。

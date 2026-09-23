# VERSIONING.md — 版本治理政策（v20.4.0-alpha · P2-16）

> **为什么成文**（六方外审 Sonnet A1 实锤）：v14.0.0 → v20.3.0 仅用 31 天，
> 8 月 6 日一天连打 3 个公开 tag、8 月 7 日连打 4 个 —— 版本号不再能作为
> 变更风险的可靠信号。v20.3.4 起已确立双轨纪律，本文把它写成政策。

## f 世代版本规格（f0.1 起 · 2026-09-23）

> **本节优先于下方「双轨制」。** 双轨制是 v 世代（v20.3.4～v22.0）的政策，
> 自 f0.1 起**大小仓统一版本号**，双轨制仅作历史留档。

### 1. 形态：`f*.*`

对外版本号、Tag、Release 一律 `f<major>.<minor>`（如 `f0.1`）。

`f` = **future / fantasy / forever**。

### 2. 两层版本，一一对应

`f0.1` **不是 PEP 440 合法版本**（PEP 440 要求数字开头），直接写进
`pyproject.toml` 会让 `pip install` 失败。故分两层：

| 层 | 常量 | 取值示例 | 用途 |
|---|---|---|---|
| 技术版本 | `SERVICE_VERSION` | `0.1.0` | `pyproject.toml` / `manifest.json` / 包管理 |
| 品牌版本 | `FULL_VERSION` | `f0.1` | 对外展示 / Tag / Release / README 宣称 |

两者由 `LINEAGE` 第三列钉死一一对应，不许各走各的；守卫
`test_readme_public_version_claim_matches_service_version` 逐字比对
README 宣称与 `FULL_VERSION`。

### 3. 递进规则

- **常规升级**（功能增强、缺陷修复、优化）：升末位 —— `f0.1 → f0.2 → f0.3`。
- **功能性颠覆性改造**：升首位 —— `f0.x → f1.0`。
- 不再使用 alpha/beta/gamma 阶段后缀；不再有三级版本号。

### 4. 单仓同号（废除双轨）

大仓（`monkey2jack/aiduMEI`）与小仓（`monkey2jack/aiduMEI_dudu`）**同版本号**。

- **小仓定位**：中间测试仓 + 代码备份仓；
- **大仓定位**：对外公开仓；
- 两仓 Tag/Release 用同一个 `f*.*`，不再出现「大仓 v20.4 / 小仓 v20.4.0」这类分叉。

### 5. 不变的铁律

- 既有公开 Tag 与 Release 永不修改、永不删除（历史 v 世代 tag 原样留存）；
- Release 标题必须是纯版本号（`f0.1`），不带代号/中文尾巴/emoji；
- 每版在 `LINEAGE` 与 `CHANGELOG.md` 留主题与实测数字。

---

## 双轨制（v 世代历史政策 · 已由上节取代）


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

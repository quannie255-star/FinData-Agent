# 通用数据可信报告 · beijing_pm25

- **数据源**：csv:beijing_pm25.csv
- **规模**：2,160 行 × 7 列
- **观察日**：2026-09-17 · schema 声明式

## 结论

**健康分 50.0（严重）** · 徽章 ✓0 ⚠️7 ✗0 · 信号 2 条

> **通用包上限声明**：无领域知识，最高只能给「✓ 基线通过」（= 没查出毛病，不是证明可靠）。「✓ 已核验」需要领域包的归因背书。

## 指标可信度（逐列徽章）

| 列 | 类型 | 徽章 | 依据 |
|---|---|---|---|
| datetime | datetime | ⚠️ 仅借鉴 | 疑点：数据停更 4917 天，超容忍 30 天 |
| pm25 | numeric | ⚠️ 仅借鉴 | 疑点：数据停更 4917 天，超容忍 30 天 |
| temp | numeric | ⚠️ 仅借鉴 | 疑点：数据停更 4917 天，超容忍 30 天 |
| dewp | numeric | ⚠️ 仅借鉴 | 疑点：数据停更 4917 天，超容忍 30 天 |
| pres | numeric | ⚠️ 仅借鉴 | 疑点：数据停更 4917 天，超容忍 30 天 |
| iws | numeric | ⚠️ 仅借鉴 | 疑点：数据停更 4917 天，超容忍 30 天 |
| cbwd | categorical | ⚠️ 仅借鉴 | 疑点：数据停更 4917 天，超容忍 30 天 |

<details>
<summary>证据链：beijing_pm25.datetime（⚠️ 仅借鉴）</summary>

  - `freshness:stale_days` — * 全表 观测 4917.0（阈值 30.0）
</details>
<details>
<summary>证据链：beijing_pm25.pm25（⚠️ 仅借鉴）</summary>

  - `freshness:stale_days` — * 全表 观测 4917.0（阈值 30.0）
</details>
<details>
<summary>证据链：beijing_pm25.temp（⚠️ 仅借鉴）</summary>

  - `freshness:stale_days` — * 全表 观测 4917.0（阈值 30.0）
</details>
<details>
<summary>证据链：beijing_pm25.dewp（⚠️ 仅借鉴）</summary>

  - `freshness:stale_days` — * 全表 观测 4917.0（阈值 30.0）
</details>
<details>
<summary>证据链：beijing_pm25.pres（⚠️ 仅借鉴）</summary>

  - `freshness:stale_days` — * 全表 观测 4917.0（阈值 30.0）
</details>
<details>
<summary>证据链：beijing_pm25.iws（⚠️ 仅借鉴）</summary>

  - `freshness:stale_days` — * 全表 观测 4917.0（阈值 30.0）
  - `outlier:outlier_rate::iws` — iws 全表 观测 0.10694444444444444（阈值 0.05）
</details>
<details>
<summary>证据链：beijing_pm25.cbwd（⚠️ 仅借鉴）</summary>

  - `freshness:stale_days` — * 全表 观测 4917.0（阈值 30.0）
</details>

## 待处理信号（2）

| 探针 | 位置 | 级别 | 根因 | 说明 |
|---|---|---|---|---|
| freshness | * | P1 | `upstream_stale` | 数据停更 4917 天，超容忍 30 天 |
| outlier | iws | P2 | `outlier_burst` | IQR 离群率 10.7% 超容忍 5%（1.5×IQR 围栏） |

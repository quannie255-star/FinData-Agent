# 通用数据可信报告 · orders

- **数据源**：csv:orders.csv
- **规模**：5,005 行 × 6 列
- **观察日**：2026-09-17 · schema 声明式

## 结论

**健康分 0.0（严重）** · 徽章 ✓0 ⚠️0 ✗6 · 信号 3 条

> **通用包上限声明**：无领域知识，最高只能给「✓ 基线通过」（= 没查出毛病，不是证明可靠）。「✓ 已核验」需要领域包的归因背书。

## 指标可信度（逐列徽章）

| 列 | 类型 | 徽章 | 依据 |
|---|---|---|---|
| order_id | id | ✗ 不可用 | duplicate_write：duplicate_keys 命中（观测 5.0，阈值 0.0） |
| user_id | id | ✗ 不可用 | duplicate_write：duplicate_keys 命中（观测 5.0，阈值 0.0） |
| category | categorical | ✗ 不可用 | duplicate_write：duplicate_keys 命中（观测 5.0，阈值 0.0） |
| amount | numeric | ✗ 不可用 | duplicate_write：duplicate_keys 命中（观测 5.0，阈值 0.0） |
| quantity | numeric | ✗ 不可用 | duplicate_write：duplicate_keys 命中（观测 5.0，阈值 0.0） |
| order_ts | datetime | ✗ 不可用 | duplicate_write：duplicate_keys 命中（观测 5.0，阈值 0.0） |

<details>
<summary>证据链：orders.order_id（✗ 不可用）</summary>

  - `uniqueness:duplicate_keys` — * 全表 观测 5.0（阈值 0.0）
</details>
<details>
<summary>证据链：orders.user_id（✗ 不可用）</summary>

  - `uniqueness:duplicate_keys` — * 全表 观测 5.0（阈值 0.0）
</details>
<details>
<summary>证据链：orders.category（✗ 不可用）</summary>

  - `uniqueness:duplicate_keys` — * 全表 观测 5.0（阈值 0.0）
</details>
<details>
<summary>证据链：orders.amount（✗ 不可用）</summary>

  - `uniqueness:duplicate_keys` — * 全表 观测 5.0（阈值 0.0）
  - `outlier:outlier_rate::amount` — amount 全表 观测 0.16219369894982497（阈值 0.05）
  - `outlier:level_shift::amount` — amount 全表 观测 3.828（阈值 3.0）
</details>
<details>
<summary>证据链：orders.quantity（✗ 不可用）</summary>

  - `uniqueness:duplicate_keys` — * 全表 观测 5.0（阈值 0.0）
</details>
<details>
<summary>证据链：orders.order_ts（✗ 不可用）</summary>

  - `uniqueness:duplicate_keys` — * 全表 观测 5.0（阈值 0.0）
</details>

## 待处理信号（3）

| 探针 | 位置 | 级别 | 根因 | 说明 |
|---|---|---|---|---|
| uniqueness | * | P0 | `duplicate_write` | duplicate_keys 命中（观测 5.0，阈值 0.0） |
| outlier | amount | P2 | `distribution_drift` | 近期均值/历史均值 = 3.83，分布疑似整体迁移 |
| outlier | amount | P1 | `distribution_drift` | 近期均值/历史均值 = 3.83，分布疑似整体迁移 |

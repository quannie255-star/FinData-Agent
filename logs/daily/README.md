# logs/daily/ —— 每日管线的运行记录（为什么它必须入库）

这个目录**原本被 `.gitignore` 排除**（`logs/` 是再生产物）。它被单独开了一个例外，
理由只有一条：**这些日志不可复现**。

- 2026-09-15 那次运行记录的是 `告警 7 / 抑制 0 / 健康分 72`。
  那是一个**已经不存在了的状态** —— 今天重跑只会得到修复后的结果（`告警 1 / 抑制 8 / 健康分 96`）。
- 所以这不是"日志"，是**本项目唯一一份"真实数据考试 → 根因修复 → 之后连续没回退"的原始凭据**。
- 可读汇总见 `examples/daily-pipeline-run-history.md`（由 `scripts/summarize_daily_runs.py` 生成）；
  报告形态见 `examples/daily-report-snapshot-2026-09-24.md`。
  注意 `reports/daily/` 仍然被 gitignore —— 那是**可重生成**的产物，与历史不同。

## 文件命名与口径

| 文件 | 写它的东西 | 编码 |
| --- | --- | --- |
| `YYYYMMDD.log` | `scripts/daily_pipeline.py` 自己（管线主日志） | UTF-8 |
| `ingest-YYYYMMDD.log` / `events-YYYYMMDD.log` | 父进程转存的子进程输出 | UTF-8（`errors="replace"`） |
| `schtasks.log` | **调度器 wrapper 的 `>>` 重定向**（累积所有次） | **GBK / cp936** |

- **引用数字一律以 `YYYYMMDD.log` 为准。** 那是管线自己写的，格式稳定。
- `schtasks.log` 是 GBK，用 UTF-8 读会直接 `UnicodeDecodeError`（不是损坏，是编码不同）。
  读它必须显式 `decode("gbk")`。它留在这里的价值是**记录了调度器会话里的那次崩溃**。

## 2026-09-15 19:17 那次崩溃（留在 `schtasks.log` 第 2–20 行）

调度器启动的第一次运行直接挂了，而**手动跑不会复现**：

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xb2 in position 0: invalid start byte
  File ".../subprocess.py", line 1552, in _readerthread
TypeError: 'NoneType' object is not subscriptable
  File ".../scripts/daily_pipeline.py", line 65, in main
```

根因：父进程按 UTF-8 解子进程输出，而子进程在调度器给的**非 UTF-8 控制台**里输出 GBK。
两分钟后（19:19）换法重跑即通过，修法落在 `scripts/daily_pipeline.py`：

```python
subprocess.run(..., text=True, encoding="utf-8", errors="replace")
```

代码里留了原因注释（「管线不许因日志崩」）。代价是子进程的 GBK 中文会被替换成乱码 ——
**这是刻意的取舍：无人值守的管线宁可乱码，不许崩。**

## 边界（别把这批日志说过头）

- 单一数据源（akshare）、25 只标的、**外部用户 0** ⇒ 自用规模，不是产品规模。
- 调度任务本身**不在本仓库注册**（Windows 计划任务）；仓库里只能看到它每次运行留下的日志。
- 非交易日管线照跑，`asof` 会停在最近一个交易日 ⇒ **`asof` 不走 ≠ 管线坏了**。

# 真实 MCP server 工具语料

这个目录里的东西**不是构造的**，是从公开来源抓下来的真实 server 工具定义。
它替换掉的是 `src/findata/contextbudget/schemas.py` 里那 23 个手写的"沉默工具"定义
（那是全项目最容易被问倒的一处，见 `docs/r5.0-acceptance.md` §4.2）。

## 文件

| 路径 | 是什么 |
| --- | --- |
| `schemas-github.json` | **加工产物**：`tools` 是可直接喂给 runner 的 OpenAI 格式清单；来源在平行的 `tool_index` 里（按下标对齐） |
| `raw/*.json` | 每个源文件的**原始字节**缓存。有它才能 `--offline` 复现，不打网络 |

## 来源与许可

- 源：`github.com/oslook/mcp-servers-schemas` 的 `schemas/`（47 个文件）
- 许可：**MIT**（`Copyright (c) 2024 oslook`，取于该仓库 `LICENSE`）
- 抓取脚本：`scripts/fetch_mcp_corpus.py`（命令见下）
- 抓取时间、逐文件的 `via`（走 api 还是 raw）、字节数与 sha256 都记在产物的
  `servers[].provenance` 里 —— 这批发的是什么、从哪来，产物自己说得清

```bash
# 抓取（首次会打网络；之后同样命令会走缓存）
uv run python scripts/fetch_mcp_corpus.py --source schemas \
    --out examples/mcp-corpus/schemas-github.json

# 只用缓存（不打网络，可复现）
uv run python scripts/fetch_mcp_corpus.py --source schemas --offline \
    --out examples/mcp-corpus/schemas-github.json
```

## 怎么用

```bash
uv run python scripts/run_context_audit.py \
    --tools-from examples/mcp-corpus/schemas-github.json --limit 2
```

账单里会**同时**记下语料自报的 `meta.tools_list_sha256` 与本次重算的指纹，并核对它们 ——
"这次实验跑的就是这份语料"因此成为一个**可被证伪**的断言，而不是一句声明。
`tools` 里刻意**不带**来源字段，就是为了让账单侧算出来的指纹与语料自报的那个能对上。

## 诚实边界（引用这批读数时必须一起引）

1. **只有一类证据**：全部是 `page_declared` —— 别人仓库里**声明**的 schema，
   **不是** server 在本机当场报的。`introspected` 那一类已实现但**没有跑**。
2. **规模**：47 台。目标区间是 100–200 台，还没到。
3. **真实数据自带缺陷，已如实保留**：230 个定义里 **13 个（5.7%）连 schema 都没有**；
   `server_info` 的键名同时存在 camelCase 与 snake_case 两种写法。
   这两条没有被"清洗掉"——它们是真实语料的一部分，也是"能不能用同一个程序批量处理"的直接约束。
4. **抓取不是零成本**：`api.github.com` 未鉴权限额 60 次/小时。实测第一次跑出
   43/47（4 个 **403 rate limit**），重试后 47/47。失败的会**单列在 `failures` 里**，
   **不会**被当成"该 server 有 0 个工具"。

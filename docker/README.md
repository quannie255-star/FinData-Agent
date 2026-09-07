# findata 数据质量监控 Agent · 演示镜像

拉起后即可在 `http://localhost:8080` 看演示站。

```bash
docker compose up --build
```

会启一个容器（uvicorn + 静态站同进程），提供：

| 路径 | 内容 |
| --- | --- |
| `GET /` | 演示站首页（自包含 HTML） |
| `GET /healthz` | 健康探针 |
| `GET /v1/docs` | FastAPI 交互文档 |
| `POST /v1/inspect` | 跑巡检（支持 asof 回放） |
| `POST /v1/eval` | 跑评测（baseline/llm/both） |
| `GET /v1/dashboard` | 拉看板 HTML |
| `POST /v1/render` | 单日报告（md/html） |

## MCP 入口（不在镜像内）

容器只跑 HTTP。MCP 是 stdio transport，由

```bash
uv run findata-mcp
```

启动；可被 Claude Desktop / Cursor 等直接挂载。详见
[README.md §MCP](../README.md#mcp-服务)。

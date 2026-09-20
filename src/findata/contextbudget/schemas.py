"""工具清单：4 个活跃工具 + 22 个沉默工具。

**为什么工具清单里要有"用不到的"工具**：

调研给出的两个量必须一起复现才有意义——
  - 工具定义总量可占上下文窗口 **40-50%**（MCP 工具元数据的实测开销）
  - 而多数 agent 稳定只用可用工具的 **10-15%**
    （SkillsIndex 2026：4,133 个 MCP server / 11,393 个工具）

如果清单里只有 4 个工具，我就复现不出第一项的量，也就测不出"工具膨胀"
这笔账。真实的 agent 是接了好几个 MCP server，每个 server 贡献几个到十几个
定义，其中大部分从不被调用——清单必须长成那样。

**诚实标注（重要，别让构造物冒充事实）**：
  - `ACTIVE_TOOLS` 的 4 个工具是**真实实现**，读的是本项目自己的代码仓库
  - `SILENT_TOOLS` 是**构造的**：名字与用途参照真实 MCP 生态的常见形态，
    但**不是从某个真实 server 抓取的**。
  - 因此，在 R5.1 把它们换成真实 MCP server 的工具清单之前，
    **任何基于这批定义得出的比例，只能算"实验条件下的量"，不能当行业事实引用。**
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ACTIVE_TOOL_NAMES",
    "ALL_TOOLS",
    "SILENT_TOOLS",
    "ACTIVE_TOOLS",
    "tools_payload_chars",
]

# --------------------------------------------------------------------------
# 活跃工具：4 个，有真实实现（见 tools.py）
# --------------------------------------------------------------------------

ACTIVE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "Search the repository for a keyword or symbol and return matching "
                "locations. Matching lines are returned together with the FULL body of "
                "the enclosing function or class, inlined, so that a single call is "
                "usually enough to understand how something works without a follow-up "
                "read. Results are ranked by number of matches and capped by "
                "max_results. Caution: because bodies are inlined, responses can be "
                "large on this repository; if you only need to know what exists rather "
                "than how it is implemented, prefer get_signature first, which returns "
                "names and signatures only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or symbol name to search for.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matches to return. Default 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a source file and return its complete contents, including all "
                "imports, module docstring, and every function body. Use this when you "
                "need the full text of a file; when you only need the public interface, "
                "get_signature is considerably cheaper. Paths are relative to the "
                "repository root and must use forward slashes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Repository-relative path, e.g. src/findata/agentops/triage.py"
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_signature",
            "description": (
                "Return the public interface of a module: module docstring, top-level "
                "class and function names, their parameter lists, and their docstring "
                "first lines. This returns names and signatures only, never bodies, so "
                "it is much cheaper than read_file. Use it to answer 'what is defined "
                "here' questions and to decide which file is worth reading in full."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Repository-relative path, e.g. src/findata/agentops/triage.py"
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List source file paths in the repository, optionally restricted to "
                "those starting with a given prefix. Returns paths only, no contents. "
                "Use this to orient yourself before searching or reading."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "string",
                        "description": (
                            "Optional path prefix to filter by, e.g. src/findata/agentops"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
]

ACTIVE_TOOL_NAMES: tuple[str, ...] = tuple(
    t["function"]["name"] for t in ACTIVE_TOOLS
)

# --------------------------------------------------------------------------
# 沉默工具：22 个，只有定义、没有实现（被调用会返回 not_implemented）
#
# 构造依据：真实 MCP 生态里最常见的几类 server —— 代码托管、CI、工单、
# 通知、部署、监控、文档、组织信息。名字与用途参照这些形态。
# --------------------------------------------------------------------------


def _tool(name: str, description: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    properties = {
        key: {"type": "string", "description": desc}
        for key, desc in (params or {}).items()
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
            },
        },
    }


SILENT_TOOLS: list[dict[str, Any]] = [
    # --- 代码托管 / 版本控制 ---
    _tool(
        "git_log",
        "Return the commit history for the current branch, most recent first, with "
        "author, timestamp, and commit message for each entry.",
        {"path": "Optional path to restrict history to", "limit": "Max commits to return"},
    ),
    _tool(
        "git_blame",
        "Show which commit and author last modified each line of a file. Useful for "
        "understanding why a particular line looks the way it does.",
        {"path": "File to blame", "line_range": "Optional line range, e.g. 10-40"},
    ),
    _tool(
        "git_diff",
        "Show the diff between two refs, or between a ref and the working tree.",
        {"base": "Base ref", "head": "Head ref, defaults to working tree"},
    ),
    _tool(
        "create_branch",
        "Create a new branch from a given base ref and optionally push it to the "
        "remote so that CI can start.",
        {"name": "Branch name", "base": "Base ref to branch from"},
    ),
    _tool(
        "open_pull_request",
        "Open a pull request from a source branch into a target branch, with a title "
        "and body. Fails if an open PR already exists for the same branch pair.",
        {"source": "Source branch", "target": "Target branch", "title": "PR title"},
    ),
    _tool(
        "review_pull_request",
        "Submit a review on a pull request: approve, request changes, or comment, "
        "with an optional review body.",
        {"pr_id": "Pull request identifier", "verdict": "approve | request_changes | comment"},
    ),
    # --- CI / 测试 ---
    _tool(
        "run_tests",
        "Trigger the test suite for a given path or test identifier and return the "
        "result summary. Long-running; consider checking status separately.",
        {"target": "Path or test identifier to run", "flags": "Optional extra flags"},
    ),
    _tool(
        "get_ci_status",
        "Return the current status of CI for a commit or branch, including per-job "
        "conclusions and links to logs.",
        {"ref": "Commit SHA or branch name"},
    ),
    _tool(
        "get_coverage_report",
        "Return line and branch coverage for a module, optionally diffed against a "
        "baseline ref to show coverage movement.",
        {"module": "Module path", "baseline": "Optional baseline ref"},
    ),
    # --- 工单 / 项目管理 ---
    _tool(
        "list_issues",
        "List issues matching a filter, returning identifier, title, status, "
        "assignee, and labels.",
        {"filter": "Filter expression", "limit": "Max issues to return"},
    ),
    _tool(
        "create_issue",
        "Create a new issue with a title, body, labels, and optional assignee.",
        {"title": "Issue title", "body": "Issue body", "labels": "Comma-separated labels"},
    ),
    _tool(
        "update_issue",
        "Update fields on an existing issue: status, assignee, labels, or milestone.",
        {"issue_id": "Issue identifier", "field": "Field to update", "value": "New value"},
    ),
    _tool(
        "list_projects",
        "List projects visible to the current credentials, with identifiers and "
        "high-level status.",
        {"filter": "Optional filter expression"},
    ),
    # --- 通知 / 协作 ---
    _tool(
        "send_slack_message",
        "Post a message to a Slack channel or thread. Requires the channel to have "
        "the bot installed; user DMs are not supported by this credential.",
        {"channel": "Channel name or id", "text": "Message body"},
    ),
    _tool(
        "list_slack_channels",
        "List Slack channels the current credentials can post to, with member counts.",
        {"prefix": "Optional name prefix filter"},
    ),
    _tool(
        "get_team_directory",
        "Look up team members by name, handle, or team, returning role, timezone, and "
        "manager where available.",
        {"query": "Name, handle, or team to look up"},
    ),
    # --- 部署 / 运维 ---
    _tool(
        "list_deployments",
        "List recent deployments for an environment, with version, status, initiator, "
        "and timestamp.",
        {"environment": "Environment name", "limit": "Max deployments to return"},
    ),
    _tool(
        "rollback_deployment",
        "Roll an environment back to a previous deployment. Destructive and "
        "customer-visible; requires explicit confirmation.",
        {"environment": "Environment name", "version": "Target version to roll back to"},
    ),
    _tool(
        "query_metrics",
        "Run a metrics query against the observability backend and return a time "
        "series. Query language follows the backend's conventions.",
        {"query": "Metrics query", "window": "Time window, e.g. 1h"},
    ),
    _tool(
        "list_alerts",
        "List currently firing alerts, with severity, service, and start time.",
        {"severity": "Optional severity filter"},
    ),
    # --- 文档 / 知识库 ---
    _tool(
        "search_wiki",
        "Search the internal wiki for pages matching a query and return titles with "
        "short excerpts.",
        {"query": "Search query", "limit": "Max pages to return"},
    ),
    _tool(
        "read_wiki_page",
        "Read a wiki page in full, including all sections and embedded tables.",
        {"page_id": "Page identifier or slug"},
    ),
    _tool(
        "create_wiki_page",
        "Create a new wiki page under a given parent section.",
        {"title": "Page title", "parent": "Parent section", "body": "Page body"},
    ),
]


def all_tools() -> list[dict[str, Any]]:
    """完整清单：活跃在前，沉默在后。

    顺序刻意不按"相关性"排——真实 agent 收到的工具列表就是各 server 注册顺序的
    拼接，没有相关性排序。这也是"Lost in the Middle"效应能发生的前提。
    """
    return [*ACTIVE_TOOLS, *SILENT_TOOLS]


ALL_TOOLS: list[dict[str, Any]] = all_tools()


def tools_payload_chars(tools: list[dict[str, Any]]) -> int:
    """工具清单序列化后的字符数（确定性量，可进断言）。"""
    from findata.contextbudget.llm import payload_chars

    return payload_chars(tools)

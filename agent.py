"""
agent.py — ADK agent definition

Defines the LlmAgent that powers GitHub automation. MCPToolset connects to
the running GitHub MCP server at startup and dynamically loads every available
GitHub tool (get_file_contents, create_issue, search_repositories, etc.) so
the LLM can call them during its reasoning loop.
"""

import os
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import (
    MCPToolset,
    StreamableHTTPConnectionParams,
)

# ── Runtime config ─────────────────────────────────────────────────────────────
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080/mcp")
GITHUB_PAT = os.getenv("GITHUB_PAT", "")

# Defaults to Gemini 2.0 Flash. To use Claude instead, set:
#   AGENT_MODEL=anthropic/claude-sonnet-4-5   and   ANTHROPIC_API_KEY=...
MODEL = os.getenv("AGENT_MODEL", "gemini-2.0-flash")


# ── GitHub OAuth scope → MCP tool permission map ───────────────────────────────
# Each key is a GitHub OAuth scope string. The value is the set of MCP tool
# names that become accessible when that scope is granted. We union together
# all sets for every scope the PAT holds to build the final permitted set.
# Reference: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps
_SCOPE_TOOL_MAP: dict[str, set[str]] = {
    "repo": {
        # `repo` is the broadest scope — full read/write on public and private repos
        "create_repository",
        "fork_repository",
        "get_repository",
        "delete_repository",
        "list_repositories_for_user",
        "get_file_contents",
        "create_or_update_file",
        "push_files",
        "delete_file",
        "create_issue",
        "list_issues",
        "get_issue",
        "update_issue",
        "add_issue_comment",
        "list_issue_comments",
        "create_pull_request",
        "list_pull_requests",
        "get_pull_request",
        "merge_pull_request",
        "update_pull_request",
        "create_pull_request_review",
        "get_pull_request_files",
        "get_pull_request_diff",
        "get_pull_request_reviews",
        "add_pull_request_review_comment",
        "list_pull_request_review_comments",
        "list_branches",
        "create_branch",
        "delete_branch",
        "list_commits",
        "get_commit",
        "create_release",
        "list_releases",
        "get_code_scanning_alert",
        "list_code_scanning_alerts",
    },
    "public_repo": {
        # Subset of `repo` — same operations but restricted to public repositories
        "create_repository",
        "fork_repository",
        "get_repository",
        "get_file_contents",
        "create_or_update_file",
        "push_files",
        "create_issue",
        "list_issues",
        "get_issue",
        "update_issue",
        "add_issue_comment",
        "create_pull_request",
        "list_pull_requests",
        "get_pull_request",
        "merge_pull_request",
        "list_branches",
        "create_branch",
        "list_commits",
        "get_commit",
        "create_release",
        "list_releases",
    },
    "read:user": {"get_authenticated_user", "list_repositories_for_user"},
    "user": {"get_authenticated_user", "list_repositories_for_user"},
    "read:org": {"list_organization_repositories", "get_organization"},
    "security_events": {
        "get_code_scanning_alert",
        "list_code_scanning_alerts",
        "get_secret_scanning_alert",
        "list_secret_scanning_alerts",
    },
    "gist": {"create_gist", "list_gists", "get_gist", "update_gist", "delete_gist"},
}


# These tools call GitHub's public search API which works with any valid token
# regardless of which scopes it holds. They are always included.
_ALWAYS_PERMITTED: set[str] = {
    "search_repositories",
    "search_code",
    "search_issues",
    "search_users",
    "search_commits",
}


def build_agent() -> LlmAgent:
    """
    Constructs the ADK LlmAgent wired up to the GitHub MCP server.

    StreamableHTTPConnectionParams tells ADK to talk to the MCP server via the
    Streamable HTTP transport (which is what the GitHub MCP server uses when
    started with the `http` argument). The Bearer token is injected into every
    MCP request so the server can authenticate with GitHub.
    """
    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=MCP_SERVER_URL,
            headers={"Authorization": f"Bearer {GITHUB_PAT}"},
        )
    )

    return LlmAgent(
        name="github_agent",
        model=MODEL,
        description=(
            "Headless GitHub agent with full access to GitHub operations "
            "via the official GitHub MCP server."
        ),
        instruction="""You are a skilled GitHub automation agent.

You have access to a comprehensive GitHub toolset covering:
  • Repositories  — create, fork, search, get details
  • Issues        — create, update, list, close
  • Pull Requests — create, list, review, merge
  • Code & Files  — read, create, update, search
  • Branches & Commits — list, compare, diff

Guidelines:
1. Be precise about which action you are taking.
2. For destructive operations (delete, merge), ensure intent is unambiguous.
3. Return structured, human-readable results.
4. If an operation is not available, say so clearly rather than guessing.
""",
        tools=[toolset],
    )

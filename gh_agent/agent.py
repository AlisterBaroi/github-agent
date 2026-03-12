"""
agent.py — ADK agent definition

Defines the LlmAgent that powers GitHub automation. MCPToolset connects to
the running GitHub MCP server at startup and dynamically loads every available
GitHub tool (get_file_contents, create_issue, search_repositories, etc.) so
the LLM can call them during its reasoning loop.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import (
    MCPToolset,
    StreamableHTTPConnectionParams,
)

# Load .env file from root
_root_env = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_root_env, override=False)


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
            url=os.getenv("MCP_SERVER_URL", "http://localhost:8082/mcp"),
            headers={"Authorization": f"Bearer {os.getenv("GITHUB_PAT")}"},
        )
    )

    return LlmAgent(
        name="github_agent",
        model=os.getenv("AGENT_MODEL", "gemini-2.5-flash"),
        description=(
            "Headless GitHub agent with full access to GitHub operations via the official GitHub MCP server."
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


root_agent = build_agent()

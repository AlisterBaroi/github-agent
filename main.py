"""
main.py — A2A-compatible FastAPI server for the GitHub Agent

Exposes three endpoints:
  GET  /.well-known/agent.json  →  A2A agent discovery (agent card)
  POST /                        →  A2A JSON-RPC 2.0 task handler
  GET  /health                  →  Kubernetes liveness / readiness probe

The A2A protocol (https://google.github.io/A2A) is implemented manually here
rather than through the a2a-sdk, keeping dependencies lean and the code fully
transparent. Only `tasks/send` (synchronous execution) is implemented, which
covers the vast majority of agent-to-agent use cases.
"""

# from typing import Any
import os, json, httpx, uuid, uvicorn, logging
from pydantic import BaseModel
from fastapi import FastAPI  # ,Request
from fastapi.responses import JSONResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from agent import build_agent


logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("github-agent")

# ── Configuration ──────────────────────────────────────────────────────────────
# AGENT_HOST should be set to this service's cluster-internal URL so that
# the agent card's `url` field is resolvable by other pods in the cluster.
AGENT_HOST = os.getenv("AGENT_HOST", "http://localhost:8000")
AGENT_VERSION = "1.0.4"
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080/mcp")
GITHUB_PAT = os.getenv("GITHUB_PAT", "")


# ── GitHub OAuth scope → MCP tool permission map ───────────────────────────────
# This map lives here in main.py because it's purely a concern of the HTTP
# server layer (the /list_all_tools endpoint). agent.py only needs to know
# how to build and run the agent — it should not know about permission logic.
#
# Each key is a GitHub OAuth scope string (the exact values GitHub returns in
# the X-OAuth-Scopes response header). The value is the set of MCP tool names
# that become accessible when that scope is granted. At query time we union
# together all sets for every scope the PAT holds to build the permitted set.
# Reference: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps
_SCOPE_TOOL_MAP: dict[str, set[str]] = {
    "repo": {
        # `repo` is the broadest scope — full read/write on public & private repos
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

# Search tools call GitHub's public search API and work with any valid token,
# regardless of which scopes it holds. Always include them unconditionally.
_ALWAYS_PERMITTED: set[str] = {
    "search_repositories",
    "search_code",
    "search_issues",
    "search_users",
    "search_commits",
}


# ── A2A request/response models ────────────────────────────────────────────────
# Defining these as Pydantic models serves two purposes:
#   1. FastAPI uses them to generate the Swagger UI form automatically
#   2. Pydantic validates incoming data and gives clear errors for bad requests
class A2AMessagePart(BaseModel):
    type: str  # e.g. "text"
    text: str | None = None  # present when type == "text"


class A2AMessage(BaseModel):
    role: str  # "user" or "agent"
    parts: list[A2AMessagePart]


class A2ATaskParams(BaseModel):
    id: str  # caller-chosen task ID
    message: A2AMessage


class A2ARequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str  # caller-chosen RPC ID
    method: str  # e.g. "tasks/send"
    params: A2ATaskParams


# ── ADK infrastructure ─────────────────────────────────────────────────────────
# These are module-level singletons, created once when the container starts.
# The Runner owns the agent and drives the tool-calling loop on each request.
# InMemorySessionService is fine for replicas=1; for multi-replica deployments
# you would swap this for a Redis-backed session store.
_session_service = InMemorySessionService()
_agent = build_agent()
_runner = Runner(
    agent=_agent,
    app_name="github_agent",
    session_service=_session_service,
)

# ── A2A Agent Card ─────────────────────────────────────────────────────────────
# Served at GET /.well-known/agent.json per the A2A specification.
# External agents or orchestrators fetch this first to understand what this
# agent can do before sending it a task.
AGENT_CARD = {
    "name": "GitHub Agent",
    "description": (
        "Headless GitHub agent with full MCP toolset access. "
        "Manages repos, issues, pull requests, files, branches, and more."
    ),
    "url": AGENT_HOST,
    "version": AGENT_VERSION,
    "capabilities": {
        "streaming": False,  # SSE streaming not yet implemented
        "pushNotifications": False,
        "stateTransitionHistory": True,
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "repo_management",
            "name": "Repository Management",
            "description": "Create, update, fork, and search GitHub repositories.",
            "tags": ["github", "repository"],
            "examples": [
                "Create a new private repository called my-service",
                "Search for repositories owned by user:alisterbaroi",
            ],
        },
        {
            "id": "issue_tracking",
            "name": "Issue Tracking",
            "description": "Create, update, list, and close GitHub issues.",
            "tags": ["github", "issues"],
            "examples": [
                "List all open issues in alisterbaroi/github-agent",
                "Create an issue titled 'Bug: 500 on login endpoint'",
            ],
        },
        {
            "id": "pull_requests",
            "name": "Pull Request Management",
            "description": "Create, review, list, and manage pull requests.",
            "tags": ["github", "pull-requests"],
            "examples": [
                "List open PRs in alisterbaroi/github-agent",
                "Get the diff for PR #12",
            ],
        },
        {
            "id": "code_operations",
            "name": "Code & File Operations",
            "description": "Read, create, update, and search files in repositories.",
            "tags": ["github", "code", "files"],
            "examples": [
                "Read the contents of README.md from my repo",
                "Search for TODO comments across the codebase",
            ],
        },
    ],
}

# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(title="GitHub Agent (A2A)", version=AGENT_VERSION)


@app.get("/.well-known/agent.json", include_in_schema=False)
async def get_agent_card():
    """
    A2A agent discovery endpoint.
    Any agent or orchestrator wishing to communicate with us fetches this
    endpoint first to understand our capabilities, skills, and call format.
    """
    return JSONResponse(content=AGENT_CARD)


@app.post("/")
# async def handle_task(request: Request):
async def handle_task(body: A2ARequest):
    """
    A2A JSON-RPC 2.0 task endpoint.
    Expected request shape for tasks/send:
    {
        "jsonrpc": "2.0",
        "id":      "test-id-01",
        "method":  "tasks/send",
        "params": {
            "id": "task-uuid-01",
            "message": {
                "role":  "user",
                "parts": [{"type": "text", "text": "List open issues in my repo username/repo_name"}]
            }
        }
    }
    """
    # body = await request.json()
    # rpc_id = body.get("id", str(uuid.uuid4()))
    # method = body.get("method", "")
    # params = body.get("params", {})
    rpc_id = body.id
    method = body.method

    log.info(f"A2A request  method={method}  rpc_id={rpc_id}")

    if method == "tasks/send":
        # return await _tasks_send(rpc_id, params)
        return await _tasks_send(rpc_id, body.params.model_dump())

    # Any unsupported JSON-RPC method returns the standard -32601 error code.
    return _rpc_error(rpc_id, -32601, f"Method '{method}' not supported")


# ── Internal helpers ───────────────────────────────────────────────────────────


async def _tasks_send(rpc_id: str, params: dict) -> JSONResponse:
    """
    Core handler for tasks/send.

    Creates an isolated ADK session for this task, runs the agent to
    completion, and wraps the result in the A2A task response format.
    """
    task_id = params.get("id", str(uuid.uuid4()))
    user_msg = params.get("message", {})
    user_text = _extract_text(user_msg)

    if not user_text:
        return _rpc_error(
            rpc_id, -32602, "Invalid params: message contained no text parts"
        )

    # Each task gets its own session to prevent state from leaking between
    # unrelated callers. For multi-turn conversations, persist this session_id
    # and have the caller echo it back in subsequent tasks/send calls.
    session_id = str(uuid.uuid4())
    await _session_service.create_session(
        app_name="github_agent",
        user_id="a2a_caller",
        session_id=session_id,
    )

    try:
        agent_reply = await _run_agent(session_id, user_text)
        log.info(f"Task {task_id} completed")

        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "id": task_id,
                    "status": {"state": "completed"},
                    # `artifacts` carries the agent's output back to the caller
                    "artifacts": [
                        {
                            "name": "agent_response",
                            "parts": [{"type": "text", "text": agent_reply}],
                        }
                    ],
                    # Full conversation history so the caller can audit the exchange
                    "history": [
                        user_msg,
                        {
                            "role": "agent",
                            "parts": [{"type": "text", "text": agent_reply}],
                        },
                    ],
                },
            }
        )

    except Exception as exc:
        log.error(f"Task {task_id} failed: {exc}", exc_info=True)
        return _rpc_error(rpc_id, -32000, f"Agent execution error: {str(exc)}")


async def _run_agent(session_id: str, user_text: str) -> str:
    """
    Drives the ADK Runner for one turn and returns the agent's final reply.

    The Runner emits a stream of events internally: the LLM deciding to call
    a tool, the tool executing against the MCP server, the result being fed
    back to the LLM, and so on. We only capture the final text response, which
    is marked by event.is_final_response() == True.
    """
    content = genai_types.Content(
        role="user",
        parts=[genai_types.Part.from_text(text=user_text)],
    )

    final_text = ""
    async for event in _runner.run_async(
        user_id="a2a_caller",
        session_id=session_id,
        new_message=content,
    ):
        # Skip intermediate events (tool calls, tool results, partial tokens)
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    final_text += part.text

    return final_text or "Task completed (agent produced no text output)."


def _extract_text(message: dict) -> str:
    """Pulls the first text part from an A2A message dict."""
    for part in message.get("parts", []):
        if isinstance(part, dict) and part.get("type") == "text":
            return part.get("text", "")
    return ""


def _rpc_error(rpc_id: str, code: int, message: str) -> JSONResponse:
    """Builds a JSON-RPC 2.0 error response."""
    return JSONResponse(
        content={
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }
    )


@app.get("/list_all_tools")
async def list_all_tools():
    """
    Queries the GitHub MCP server directly for its full tool catalogue that is accessable for the provided GitHub Personal Access Token (PAT).

    Each tool in the response includes its name, description, and input schema,
    which tells you exactly what arguments it expects.
    """
    mcp_headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Content-Type": "application/json",
    }
    mcp_payload = {
        "jsonrpc": "2.0",
        "id": "list-tools",
        "method": "tools/list",
        "params": {},
    }

    try:
        # ── Step 1: get all tools from the MCP server ──────────────────────────
        async with httpx.AsyncClient(timeout=15.0) as client:
            mcp_resp = await client.post(
                MCP_SERVER_URL, json=mcp_payload, headers=mcp_headers
            )
            mcp_resp.raise_for_status()

        all_tools: list[dict] = (
            _parse_mcp_response(mcp_resp.text).get("result", {}).get("tools", [])
        )
        all_tool_names: set[str] = {t.get("name") for t in all_tools}

        # ── Step 2: resolve which tools this PAT is allowed to call ───────────
        perms = await _resolve_pat_permissions(all_tool_names)

        permitted_names = perms["permitted"]
        unverified_names = perms["unverified"]

        # ── Step 3: filter the MCP list and build the response ─────────────────
        permitted_tools = [
            {
                "name": t.get("name"),
                "description": t.get("description"),
                "inputSchema": t.get("inputSchema"),
            }
            for t in all_tools
            if t.get("name") in permitted_names
        ]

        result: dict = {
            "token_type": perms["token_type"],
            "granted_scopes": perms["granted_scopes"],
            "permitted_total": len(permitted_tools),
            "permitted_tools": permitted_tools,
        }

        # Surface unverified tools separately rather than silently hiding them.
        # These are tools the MCP server offers but that have no entry in our
        # scope map — they may or may not work depending on the token.
        if unverified_names:
            result["unverified_tools"] = {
                "note": (
                    "These tools exist on the MCP server but have no entry in "
                    "the scope map. They may work depending on your token's "
                    "permissions."
                ),
                "names": sorted(unverified_names),
            }

        if perms["token_type"] == "fine-grained":
            result["warning"] = (
                "Fine-grained PATs use per-repo permissions that cannot be "
                "fully introspected via the OAuth scopes API. The permitted "
                "list was inferred from lightweight read probes and may not "
                "reflect write permissions accurately."
            )

        return result

    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 401:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "GitHub rejected the PAT — it may be invalid or expired."
                },
            )
        return JSONResponse(
            status_code=502,
            content={
                "error": f"Upstream returned {status}",
                "detail": exc.response.text,
            },
        )
    except Exception as exc:
        log.error(f"list_all_tools failed: {exc}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ── Permission resolution helper ───────────────────────────────────────────────


async def _resolve_pat_permissions(all_tool_names: set[str]) -> dict:
    """
    Determines which MCP tools the current PAT is actually permitted to call.

    Classic PATs expose their scopes in the X-OAuth-Scopes response header on
    any authenticated GitHub API call. We hit GET /user (lightweight, no side
    effects) and read that header to get the full list of granted scopes, then
    union together the tool sets for each scope from _SCOPE_TOOL_MAP.

    Fine-grained PATs return an empty X-OAuth-Scopes header because they use
    a completely different, per-resource permission model. For those we probe
    GET /user/repos to infer whether repo-level access exists, grant the
    repo tool set conservatively, and attach a warning to the response.
    """
    gh_headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        user_resp = await client.get("https://api.github.com/user", headers=gh_headers)
        user_resp.raise_for_status()

    scopes_header = user_resp.headers.get("X-OAuth-Scopes", "")
    is_fine_grained = not scopes_header
    granted_scopes = (
        [s.strip() for s in scopes_header.split(",") if s.strip()]
        if scopes_header
        else []
    )

    # Union together every tool set for each scope the PAT holds.
    permitted: set[str] = set(_ALWAYS_PERMITTED)
    for scope in granted_scopes:
        permitted |= _SCOPE_TOOL_MAP.get(scope, set())

    if is_fine_grained:
        # We can't read fine-grained scopes directly, so probe a cheap
        # read-only endpoint. A 200 means Contents:read is granted, which
        # covers most repo-level tools. Write permissions remain unverifiable
        # without attempting an actual write operation.
        async with httpx.AsyncClient(timeout=10.0) as client:
            probe = await client.get(
                "https://api.github.com/user/repos?per_page=1",
                headers=gh_headers,
            )
        if probe.status_code == 200:
            permitted |= _SCOPE_TOOL_MAP["repo"]

    # Identify tools the MCP server offers that have no entry in our map at
    # all — these get surfaced as "unverified" rather than silently dropped.
    all_mapped: set[str] = set(_ALWAYS_PERMITTED)
    for tools in _SCOPE_TOOL_MAP.values():
        all_mapped |= tools

    return {
        "token_type": "fine-grained" if is_fine_grained else "classic",
        "granted_scopes": granted_scopes,
        # Intersect with actual MCP names so we never surface phantom entries
        "permitted": permitted & all_tool_names,
        "unverified": all_tool_names - all_mapped,
    }


def _parse_mcp_response(text: str) -> dict:
    """
    Extracts the JSON payload from an SSE-formatted MCP response.
    SSE responses prefix each data line with 'data: ', so we strip that
    prefix and parse what remains. Falls back to plain JSON if needed.
    """

    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(text)  # fallback for plain JSON responses


@app.get("/health")
async def health_check():
    """Kubernetes liveness and readiness probe."""
    return {
        "status": "ok",
        "message": "GitHub Agent is running",
        "version": AGENT_VERSION,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

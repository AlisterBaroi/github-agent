"""
tools_catalogue.py — MCP tool discovery with PAT-based permission filtering

This module owns everything related to the /list_all_tools endpoint:
  - The GitHub OAuth scope → MCP tool name mapping (_SCOPE_TOOL_MAP)
  - The always-permitted search tools (_ALWAYS_PERMITTED)
  - The permission resolution logic (_resolve_pat_permissions)
  - The SSE response parser (_parse_mcp_response)
  - The APIRouter that exposes the /list_all_tools route

main.py registers this router with a single include_router() call and never
needs to know about the internals here.
"""

import json, logging, os, httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger("github-agent")

# ── Runtime config (read from the same env vars as main.py) ───────────────────
# Each module reads the env vars it needs independently. This is intentional —
# Python modules have isolated namespaces, so variables don't leak across files.
# MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080/mcp")
# GITHUB_PAT = os.getenv("GITHUB_PAT")


# ── GitHub OAuth scope → MCP tool permission map ──────────────────────────────
# Each key is a GitHub OAuth scope string (the exact values returned in the
# X-OAuth-Scopes response header). The value is the set of MCP tool names
# that become accessible when that scope is granted.
#
# At query time we union together all sets for every scope the PAT holds to
# build the complete permitted set for that token.
#
# Reference: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps
_SCOPE_TOOL_MAP: dict[str, set[str]] = {
    "repo": {
        # The broadest scope — full read/write on both public and private repos.
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
        # Same operations as `repo` but restricted to public repositories only.
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

# Search tools call GitHub's public search API, which works with any valid
# token regardless of which OAuth scopes it holds. Always include them.
_ALWAYS_PERMITTED: set[str] = {
    "search_repositories",
    "search_code",
    "search_issues",
    "search_users",
    "search_commits",
}


# ── APIRouter ─────────────────────────────────────────────────────────────────
# Routes defined on this router are registered onto the main FastAPI app via
# app.include_router(tools_router) in main.py. From the outside, the endpoint
# is still reachable at GET /list_all_tools — the router is transparent.
tools_router = APIRouter()


@tools_router.get("/list_all_tools")
async def list_all_tools():
    """
    Returns only the MCP tools the current PAT is permitted to call.

    The MCP server always advertises its full tool catalogue regardless of
    token permissions — enforcement only happens at the GitHub API layer when
    a tool is actually invoked. This endpoint closes that gap by:
      1. Fetching the full tool list from the MCP server.
      2. Asking GitHub's API which OAuth scopes this PAT holds.
      3. Cross-referencing those scopes against _SCOPE_TOOL_MAP to build the
         permitted set, then filtering the tool list down to that set.

    Fine-grained PATs return an empty X-OAuth-Scopes header because they use
    a per-repo, per-permission model instead of OAuth scopes. For those we
    fall back to a lightweight read probe and flag the result accordingly.
    """
    mcp_headers = {
        "Authorization": f"Bearer {os.getenv("GITHUB_PAT")}",
        "Content-Type": "application/json",
    }
    mcp_payload = {
        "jsonrpc": "2.0",
        "id": "list-tools",
        "method": "tools/list",
        "params": {},
    }

    try:
        # ── Step 1: fetch every tool the MCP server currently offers ──────────
        async with httpx.AsyncClient(timeout=15.0) as client:
            mcp_resp = await client.post(
                os.getenv("MCP_SERVER_URL", "http://localhost:8080/mcp"),
                json=mcp_payload,
                headers=mcp_headers,
            )
            mcp_resp.raise_for_status()

        all_tools: list[dict] = (
            _parse_mcp_response(mcp_resp.text).get("result", {}).get("tools", [])
        )
        all_tool_names: set[str] = {t.get("name") for t in all_tools}

        # ── Step 2: resolve which of those tools this PAT can actually call ───
        perms = await _resolve_pat_permissions(all_tool_names)

        # ── Step 3: filter and build the response ─────────────────────────────
        permitted_tools = [
            {
                "name": t.get("name"),
                "description": t.get("description"),
                "inputSchema": t.get("inputSchema"),
            }
            for t in all_tools
            if t.get("name") in perms["permitted"]
        ]

        result: dict = {
            "token_type": perms["token_type"],
            "granted_scopes": perms["granted_scopes"],
            "permitted_total": len(permitted_tools),
            "permitted_tools": permitted_tools,
        }

        # Surface unverified tools separately so nothing is silently hidden.
        # These are MCP tools that exist but have no entry in our scope map —
        # they may or may not work depending on the token.
        if perms["unverified"]:
            result["unverified_tools"] = {
                "note": (
                    "These tools exist on the MCP server but have no entry in "
                    "the scope map. They may work depending on your token's permissions."
                ),
                "names": sorted(perms["unverified"]),
            }

        if perms["token_type"] == "fine-grained":
            result["warning"] = (
                "Fine-grained PATs use per-repo permissions that cannot be fully "
                "introspected via the OAuth scopes API. The permitted list was "
                "inferred from lightweight read probes and may not reflect write "
                "permissions accurately."
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


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _resolve_pat_permissions(all_tool_names: set[str]) -> dict:
    """
    Determines which MCP tools the current PAT is actually permitted to call.

    Classic PATs expose their scopes in the X-OAuth-Scopes header on any
    authenticated GitHub API response. We hit GET /user (lightweight, no side
    effects) to read that header, then union together the tool sets for each
    granted scope from _SCOPE_TOOL_MAP.

    Fine-grained PATs return an empty X-OAuth-Scopes header — they use a
    completely different per-resource permission model. For those we probe
    GET /user/repos to infer whether repo-level access exists, grant the
    repo tool set conservatively, and attach a warning to the response.
    """
    gh_headers = {
        "Authorization": f"Bearer {os.getenv("GITHUB_PAT")}",
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

    # Union together the tool set for every scope the PAT holds.
    permitted: set[str] = set(_ALWAYS_PERMITTED)
    for scope in granted_scopes:
        permitted |= _SCOPE_TOOL_MAP.get(scope, set())

    if is_fine_grained:
        # Probe a cheap read-only endpoint. A 200 means Contents:read is
        # granted, which covers most repo-level tools. Write permissions
        # remain unverifiable without attempting an actual write operation.
        async with httpx.AsyncClient(timeout=10.0) as client:
            probe = await client.get(
                "https://api.github.com/user/repos?per_page=1", headers=gh_headers
            )
        if probe.status_code == 200:
            permitted |= _SCOPE_TOOL_MAP["repo"]

    # Build the set of all tools our map knows about so we can identify any
    # MCP tools that fall outside it — those are surfaced as "unverified".
    all_mapped: set[str] = set(_ALWAYS_PERMITTED)
    for tools in _SCOPE_TOOL_MAP.values():
        all_mapped |= tools

    return {
        "token_type": "fine-grained" if is_fine_grained else "classic",
        "granted_scopes": granted_scopes,
        # Intersect with actual MCP names so we never surface phantom entries.
        "permitted": permitted & all_tool_names,
        "unverified": all_tool_names - all_mapped,
    }


def _parse_mcp_response(text: str) -> dict:
    """
    Extracts the JSON payload from an SSE-formatted MCP response.

    The GitHub MCP server prefixes each data line with 'data: ' per the SSE
    spec. We strip that prefix before JSON-parsing. Falls back to treating the
    whole response as plain JSON if no SSE prefix is found (defensive).
    """
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(text)

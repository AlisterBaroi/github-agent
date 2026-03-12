"""
tools_catalogue.py — MCP tool discovery

This module owns everything related to the /list_all_tools endpoint:
  - The SSE response parser (_parse_mcp_response)
  - The APIRouter that exposes the /list_all_tools route

main.py registers this router with a single include_router() call and never
needs to know about the internals here.
"""

import json, logging, os, httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger("github-agent")


# This route is registered onto the main FastAPI app via app.include_router(tools_router) in main.py.
tools_router = APIRouter()


@tools_router.get("/list_all_tools")
async def list_all_tools():
    """
    **Returns a list of every tools the GitHub MCP Server has:**

    Fetches the full tool catalogue from the MCP server via JSON-RPC
    tools/list and returns each tool's name, description, and input schema.
    """
    mcp_headers = {
        "Authorization": f"Bearer {os.getenv('GITHUB_PAT')}",
        "Content-Type": "application/json",
    }
    mcp_payload = {
        "jsonrpc": "2.0",
        "id": "list-tools",
        "method": "tools/list",
        "params": {},
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            mcp_resp = await client.post(
                os.getenv("MCP_SERVER_URL"),
                json=mcp_payload,
                headers=mcp_headers,
            )
            mcp_resp.raise_for_status()

        all_tools: list[dict] = (
            _parse_mcp_response(mcp_resp.text).get("result", {}).get("tools", [])
        )

        return {
            "total": len(all_tools),
            "tools": [
                {
                    "name": t.get("name"),
                    "description": t.get("description"),
                    "inputSchema": t.get("inputSchema"),
                }
                for t in all_tools
            ],
        }

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

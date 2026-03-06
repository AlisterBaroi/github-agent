from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import httpx, json, os
from dotenv import load_dotenv

# Load environment variables from a .env file (if one exists)
load_dotenv()

app = FastAPI(title="GitHub Agent")

# Add the "Authorize" padlock button to the Swagger UI
security = HTTPBearer()

# Pointing directly to the Streamable HTTP endpoint
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080/mcp")


class ToolRequest(BaseModel):
    tool_name: str
    arguments: dict = {}


def parse_sse_response(response_text: str):
    """Extracts the JSON payload from an SSE formatted response string."""
    for line in response_text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])  # parse everything after 'data: '

    # Fallback if the server actually returned standard JSON
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500, detail=f"Could not parse response: {response_text}"
        )


@app.get("/tools")
# async def list_tools(authorization: str = Header(None)):
async def list_tools(credentials: HTTPAuthorizationCredentials = Depends(security)):
    # if not authorization:
    # raise HTTPException(status_code=401, detail="Missing Authorization header")
    # headers = {"Authorization": authorization, "Content-Type": "application/json"}
    # HTTPBearer automatically extracts the token from the UI's padlock
    headers = {
        "Authorization": f"Bearer {credentials.credentials}",
        "Content-Type": "application/json",
    }

    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(MCP_SERVER_URL, json=payload, headers=headers)

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        return parse_sse_response(response.text)


@app.post("/run-tool")
# async def run_github_tool(request: ToolRequest, authorization: str = Header(None)):
async def run_github_tool(
    request: ToolRequest, credentials: HTTPAuthorizationCredentials = Depends(security)
):
    # if not authorization:
    #     raise HTTPException(status_code=401, detail="Missing Authorization header")
    # headers = {"Authorization": authorization, "Content-Type": "application/json"}
    headers = {
        "Authorization": f"Bearer {credentials.credentials}",
        "Content-Type": "application/json",
    }

    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": request.tool_name, "arguments": request.arguments},
    }

    async with httpx.AsyncClient(
        timeout=60.0
    ) as client:  # Longer timeout for tool execution
        response = await client.post(MCP_SERVER_URL, json=payload, headers=headers)

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        return parse_sse_response(response.text)


@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "Agent is running"}

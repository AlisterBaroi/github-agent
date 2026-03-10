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
import os, uuid, uvicorn, logging
from pydantic import BaseModel
from fastapi import FastAPI  # ,Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from gh_agent.agent import build_agent
from tools_catalogue import tools_router


logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("github-agent")

# ── Configuration ──────────────────────────────────────────────────────────────
# AGENT_HOST should be set to this service's cluster-internal URL so that
# the agent card's `url` field is resolvable by other pods in the cluster.
# AGENT_HOST = os.getenv("AGENT_HOST", "http://localhost:8000")
AGENT_VERSION = os.getenv("AGENT_VERSION", "1.0.8")

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


class MessageRequest(BaseModel):
    """Simple single-field input — the only thing callers need to send."""

    message: str


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
    "url": os.getenv("AGENT_HOST", "http://localhost:8000"),
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


# Add this immediately after app = FastAPI(...):
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to ["http://localhost:8080"] if preferred
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/.well-known/agent.json", include_in_schema=False)
async def get_agent_card():
    """
    A2A agent discovery endpoint.
    Any agent or orchestrator wishing to communicate with us fetches this
    endpoint first to understand our capabilities, skills, and call format.
    """
    return JSONResponse(content=AGENT_CARD)


@app.post("/", summary="Run a task", response_description="Agent reply")
async def handle_task(body: MessageRequest):
    """
    Send a plain-English message to the GitHub agent.

    ```json
    {"message": "List all open issues in alisterbaroi/github-agent"}
    ```
    """
    log.info(f"message request: {body.message!r}")

    session_id = str(uuid.uuid4())
    await _session_service.create_session(
        app_name="github_agent",
        user_id="a2a_caller",
        session_id=session_id,
    )

    try:
        reply = await _run_agent(session_id, body.message)
        return {"reply": reply}
    except Exception as exc:
        log.error(f"Agent error: {exc}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/a2a", summary="A2A JSON-RPC 2.0 (legacy)")
# async def handle_task(request: Request):
async def handle_a2a_task(body: A2ARequest):
    """
    A2A JSON-RPC 2.0 task endpoint.
    Expected request shape for tasks/send:
    ```json
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
    ```
    """
    # rpc_id = body.id
    # method = body.method

    log.info(f"A2A request  method={body.method}  rpc_id={body.id}")

    if body.method == "tasks/send":
        return await _tasks_send(body.id, body.params.model_dump())

    # Any unsupported JSON-RPC method returns the standard -32601 error code.
    return _rpc_error(body.id, -32601, f"Method '{body.method}' not supported")


# ── Internal helpers ───────────────────────────────────────────────────────────


async def _tasks_send(rpc_id: str, params: dict) -> JSONResponse:
    """
    Core handler for tasks/send. Creates an isolated ADK session for this task, runs the agent to completion, and wraps the result in A2A task response format.
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


# Register the tools catalogue router. FastAPI merges its routes (/list_all_tools)
# into the main app transparently — callers see no difference from the outside.
app.include_router(tools_router)


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

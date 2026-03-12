"""
A2A-compatible FastAPI server for the GitHub Agent
Endpoints:
  POST /                              →  A2A JSON-RPC 2.0 (message/send, message/stream, tasks/get, tasks/cancel)
  GET  /.well-known/agent-card.json   →  A2A agent card (canonical)
  GET  /.well-known/agent.json        →  A2A agent card (backward compat)
  POST /message                       →  Simple message endpoint (non-A2A convenience)
  GET  /healthz                       →  Kubernetes liveness probe
  GET  /readyz                        →  Kubernetes readiness probe (deep dependency checks)
  GET  /list_all_tools                →  MCP tool catalogue
A2A protocol support is provided by google-adk's built-in A2A integration (A2aAgentExecutor + A2AFastAPIApplication), giving full spec compliance: message/send, message/stream (SSE), tasks/get, tasks/cancel, proper task
state machine, task persistence, and a spec-compliant agent card.
"""

import os, uuid, time, uvicorn, logging, httpx
from pydantic import BaseModel
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.genai import types as genai_types
from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPI, A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, InMemoryPushNotificationConfigStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill
from gh_agent.agent import build_agent
from tools_catalogue import tools_router


logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("github-agent")
AGENT_VERSION = os.getenv("AGENT_VERSION", "1.1.0")
_start_time = time.time()
_REQUIRED_ENV_VARS = ["GITHUB_PAT", "MCP_SERVER_URL", "AGENT_HOST", "GOOGLE_API_KEY"]

# ── ADK infrastructure: Module-level singletons, created once when the container starts
_agent = build_agent()
# Fine for replicas=1; for multi-replica deployments, need would swap this with Redis-backed session store.
_session_service = InMemorySessionService()
# Owns the agent and drives the tool-calling loop on each request.
_runner = Runner(
    agent=_agent,
    app_name="github_agent",
    session_service=_session_service,
)

# ── A2A Agent Card: External agents/orchestrators fetch this at GET /.well-known/agent-card.json to see agent's specialty before sending tasks.
_agent_card = AgentCard(  # a2a-sdk's AgentCard model for full spec compliance.
    name="GitHub Agent",
    description=(
        "Headless GitHub agent with full MCP toolset access. "
        "Manages repos, issues, pull requests, files, branches, and more."
    ),
    url=os.getenv("AGENT_HOST"),
    version=AGENT_VERSION,
    capabilities=AgentCapabilities(
        streaming=True,
        push_notifications=False,
        state_transition_history=True,
    ),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    skills=[
        AgentSkill(
            id="repo_management",
            name="Repository Management",
            description="Create, update, fork, and search GitHub repositories.",
            tags=["github", "repository"],
            examples=[
                "Create a new private repository called my-service",
                "Search for repositories owned by user:alisterbaroi",
            ],
        ),
        AgentSkill(
            id="issue_tracking",
            name="Issue Tracking",
            description="Create, update, list, and close GitHub issues.",
            tags=["github", "issues"],
            examples=[
                "List all open issues in alisterbaroi/github-agent",
                "Create an issue titled 'Bug: 500 on login endpoint'",
            ],
        ),
        AgentSkill(
            id="pull_requests",
            name="Pull Request Management",
            description="Create, review, list, and manage pull requests.",
            tags=["github", "pull-requests"],
            examples=[
                "List open PRs in alisterbaroi/github-agent",
                "Get the diff for PR #12",
            ],
        ),
        AgentSkill(
            id="code_operations",
            name="Code & File Operations",
            description="Read, create, update, and search files in repositories.",
            tags=["github", "code", "files"],
            examples=[
                "Read the contents of README.md from my repo",
                "Search for TODO comments across the codebase",
            ],
        ),
    ],
)

# ── A2A wiring: Wire the ADK Runner into the A2A protocol stack (Runner → A2aAgentExecutor → DefaultRequestHandler → A2AFastAPIApplication)
_task_store = InMemoryTaskStore()
_agent_executor = A2aAgentExecutor(runner=_runner)
_request_handler = DefaultRequestHandler(
    agent_executor=_agent_executor,
    task_store=_task_store,
    push_config_store=InMemoryPushNotificationConfigStore(),
)
_a2a_app = A2AFastAPIApplication(
    agent_card=_agent_card,
    http_handler=_request_handler,
)

# ── FastAPI application
app = A2AFastAPI(title="GitHub Agent (A2A)", version=AGENT_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to specific origins if preferred
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add A2A routes: POST /, GET /.well-known/agent-card.json, GET /.well-known/agent.json
_a2a_app.add_routes_to_app(app)

# Patch the OpenAPI schema to add a custom description and example to the A2A POST / endpoint.
_original_openapi = app.openapi


def _custom_openapi():
    schema = _original_openapi()
    if "/" in schema.get("paths", {}):
        post = schema["paths"]["/"]["post"]
        post["summary"] = post.get("summary", "") + " (A2A JSON-RPC 2.0)"
        post["description"] = (
            post.get("description", "") + "\n\n"
            "A2A protocol endpoint. Supports **message/send**, **message/stream**, "
            "**tasks/get**, and **tasks/cancel**." + "\n\n"
            "Example request **(message/send)**:\n"
            "```json\n"
            "{\n"
            '  "id": "test-001",\n'
            '  "jsonrpc": "2.0",\n'
            '  "method": "message/send",\n'
            '  "params": {\n'
            '    "message": {\n'
            '      "messageId": "msg-001",\n'
            '      "role": "user",\n'
            '      "parts": [\n'
            "        {\n"
            '          "kind": "text",\n'
            '          "text": "List all open issues in username/repository"\n'
            "        }\n"
            "      ]\n"
            "    }\n"
            "  }\n"
            "}\n"
            "```"
        )
    return schema


app.openapi = _custom_openapi


# ── Simple message endpoint (non-A2A convenience)
class MessageRequest(BaseModel):
    """Simple single-field input — the only thing callers need to send."""

    message: str


@app.post("/message", summary="Simple Message", response_description="Agent reply")
async def handle_message(body: MessageRequest):
    """
    Send a plain-English message to the GitHub agent (non-A2A endpoint, with no memory/state) for UI testing convenience.
    ```json
    {"message": "List all open issues at the username/repository"}
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


# ── Internal helpers
async def _run_agent(session_id: str, user_text: str) -> str:
    """
    Drives the ADK Runner for one turn and returns the agent's final reply.
    Used by the /message convenience endpoint.
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
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    final_text += part.text

    return final_text or "Task completed (agent produced no text output)."


# Register the tools catalogue router.
app.include_router(tools_router)


@app.get("/healthz")
async def liveness():
    """Kubernetes liveness probe — is the process alive?"""
    return {
        "status": "ok",
        "version": AGENT_VERSION,
        "uptime_seconds": round(time.time() - _start_time, 2),
    }


@app.get("/readyz")
async def readiness():
    """Kubernetes readiness probe — are all dependencies functional?"""
    checks: dict[str, str] = {}
    # 1. Environment configuration
    missing_vars = [v for v in _REQUIRED_ENV_VARS if not os.getenv(v)]
    checks["env_config"] = (
        "ok" if not missing_vars else f"missing: {', '.join(missing_vars)}"
    )
    # 2. ADK Runner & Agent readiness
    checks["agent"] = "ok" if _agent is not None else "not initialized"
    checks["runner"] = "ok" if _runner is not None else "not initialized"
    # 3. Session service
    try:
        probe_sid = f"_healthcheck_{uuid.uuid4()}"
        await _session_service.create_session(
            app_name="github_agent",
            user_id="_healthcheck",
            session_id=probe_sid,
        )
        await _session_service.delete_session(
            app_name="github_agent",
            user_id="_healthcheck",
            session_id=probe_sid,
        )
        checks["session_service"] = "ok"
    except Exception as exc:
        checks["session_service"] = f"error: {exc}"
    # 4. Task store
    try:
        _task_store.get if callable(getattr(_task_store, "get", None)) else None
        checks["task_store"] = "ok"
    except Exception as exc:
        checks["task_store"] = f"error: {exc}"
    # 5. GitHub MCP connectivity
    mcp_url = os.getenv("MCP_SERVER_URL", "http://localhost:8082/mcp")
    github_pat = os.getenv("GITHUB_PAT", "")
    if not github_pat:
        checks["github_mcp"] = "skipped: GITHUB_PAT not set"
    else:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    mcp_url,
                    headers={
                        "Authorization": f"Bearer {github_pat}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": "health",
                        "method": "tools/list",
                        "params": {},
                    },
                )
            checks["github_mcp"] = (
                "ok" if resp.status_code == 200 else f"http {resp.status_code}"
            )
        except Exception as exc:
            checks["github_mcp"] = f"unreachable: {exc}"
    # Overall status
    all_ok = all(v == "ok" for v in checks.values())
    status_code = 200 if all_ok else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if all_ok else "degraded",
            "version": AGENT_VERSION,
            "uptime_seconds": round(time.time() - _start_time, 2),
            "checks": checks,
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

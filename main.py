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

import os, uuid, uvicorn, logging
from pydantic import BaseModel
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.genai import types as genai_types
from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPI
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, InMemoryPushNotificationConfigStore
from gh_agent.agent import build_agent
from tools_catalogue import tools_router
from utilities import health_router, init_health_dependencies, agent_card, patch_openapi, A2AFastAPIApplicationWithHTTPErrors


logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("github-agent")
AGENT_VERSION = os.getenv("AGENT_VERSION", "1.1.0")

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

# ── A2A wiring: Wire the ADK Runner into the A2A protocol stack (Runner → A2aAgentExecutor → DefaultRequestHandler → A2AFastAPIApplication)
_task_store = InMemoryTaskStore()
_agent_executor = A2aAgentExecutor(runner=_runner)
_request_handler = DefaultRequestHandler(
    agent_executor=_agent_executor,
    task_store=_task_store,
    push_config_store=InMemoryPushNotificationConfigStore(),
)
_a2a_app = A2AFastAPIApplicationWithHTTPErrors(
    agent_card=agent_card,
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

patch_openapi(app)


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


# Register routers
app.include_router(tools_router)
init_health_dependencies(_agent, _runner, _session_service, _task_store)
app.include_router(health_router)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

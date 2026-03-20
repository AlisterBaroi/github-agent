import os, uuid, time, httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from a2a.types import AgentCard, AgentCapabilities, AgentSkill, A2AError, JSONRPCError
from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication

health_router = APIRouter()

AGENT_VERSION = os.getenv("AGENT_VERSION", "1.1.0")
_start_time = time.time()

class A2AFastAPIApplicationWithHTTPErrors(A2AFastAPIApplication):
    _JSONRPC_TO_HTTP = {
        -32700: 400,  # Parse error
        -32600: 400,  # Invalid request
        -32602: 400,  # Invalid params
        -32601: 404,  # Method not found
        -32603: 500,  # Internal error
    }

    def _generate_error_response(self, request_id, error):
        response = super()._generate_error_response(request_id, error)
        code = error.root.code if isinstance(error, A2AError) else error.code
        response.status_code = self._JSONRPC_TO_HTTP.get(code, 200)
        return response

# ── A2A Agent Card
agent_card = AgentCard(
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
_REQUIRED_ENV_VARS = ["GITHUB_PAT", "MCP_SERVER_URL", "AGENT_HOST", "GOOGLE_API_KEY"]


def patch_openapi(app):
    """Add a custom description and example to the A2A POST / endpoint."""
    original_openapi = app.openapi

    def _custom_openapi():
        if app.openapi_schema:  # cache the schema, to prevent UI bugs
            return app.openapi_schema
        schema = original_openapi()
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
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi


# These are injected at startup via init_health_dependencies()
_agent = None
_runner = None
_session_service = None
_task_store = None


def init_health_dependencies(agent, runner, session_service, task_store):
    """Call once at startup to wire in the singletons the probes need."""
    global _agent, _runner, _session_service, _task_store
    _agent = agent
    _runner = runner
    _session_service = session_service
    _task_store = task_store


@health_router.get("/healthz")
async def liveness():
    """Kubernetes liveness probe — is the process alive?"""
    return {
        "status": "ok",
        "version": AGENT_VERSION,
        "uptime_seconds": round(time.time() - _start_time, 2),
    }


@health_router.get("/readyz")
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

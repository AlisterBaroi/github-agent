#!/bin/bash

# start.sh — launches the FastAPI server, and optionally the ADK web UI.
# Ports:
#   8000 — FastAPI (A2A, Swagger UI)
#   8001 — ADK Web UI (interactive web chat for dev/test, when enabled)
#
# Environment:
#   DEV_MODE — set to "true" to also start the ADK Web UI (default: false)

set -e  # exit immediately if any setup command fails before the servers start

echo "Starting FastAPI (uvicorn) on port 8000..."
uvicorn main:app --host 0.0.0.0 --port 8000 &
UVICORN_PID=$!

if [ "${DEV_MODE}" = "true" ]; then
    echo "Starting ADK Web UI on port 8001..."
    # adk web must be run from /app so it can find the gh_agent package directory
    adk web --host 0.0.0.0 --port 8001 &
    ADK_PID=$!

    echo "Both processes running:"
    echo "  FastAPI  PID=${UVICORN_PID}  → http://0.0.0.0:8000/docs"
    echo "  ADK Web  PID=${ADK_PID}      → http://0.0.0.0:8001"

    # Block until either process exits, then kill the other for clean shutdown.
    wait -n $UVICORN_PID $ADK_PID
    EXIT_CODE=$?

    echo "A process exited (exit code: ${EXIT_CODE}). Shutting down remaining processes..."
    kill $UVICORN_PID $ADK_PID 2>/dev/null
else
    echo "FastAPI running (ADK Web UI disabled):"
    echo "  FastAPI  PID=${UVICORN_PID}  → http://0.0.0.0:8000/docs"

    wait $UVICORN_PID
    EXIT_CODE=$?
fi

exit $EXIT_CODE
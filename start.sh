#!/bin/bash

# start.sh — launches both the FastAPI server and the ADK web UI in parallel.
# Ports:
#   8000 — FastAPI (A2A, Swagger UI)
#   8001 — ADK Web UI (interactive web chat for dev/test)

set -e  # exit immediately if any setup command fails before the servers start

echo "Starting FastAPI (uvicorn) on port 8000..."
uvicorn main:app --host 0.0.0.0 --port 8000 &
UVICORN_PID=$!

echo "Starting ADK Web UI on port 8001..."
# adk web must be run from /app so it can find the gh_agent package directory
adk web --host 0.0.0.0 --port 8001 &
ADK_PID=$!

echo "Both processes running:"
echo "  FastAPI  PID=${UVICORN_PID}  → http://0.0.0.0:8000/docs"
echo "  ADK Web  PID=${ADK_PID}      → http://0.0.0.0:8001"

# Block until either process exits. `wait -n` returns as soon as the first child finishes. 
# Then kill the other process before exiting so the container doesn't linger with a half-running state, and Kubernetes can cleanly restart the pod.
wait -n $UVICORN_PID $ADK_PID
EXIT_CODE=$?

echo "A process exited (exit code: ${EXIT_CODE}). Shutting down remaining processes..."
kill $UVICORN_PID $ADK_PID 2>/dev/null

exit $EXIT_CODE
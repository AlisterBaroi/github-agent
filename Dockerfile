# Stage 1: Build dependencies with uv

# Use official lightweight uv image
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Copy dependency files first (layer caching)
COPY pyproject.toml uv.lock ./

# Install dependencies into a standalone venv, no project install yet
RUN uv sync --frozen --no-install-project --no-dev

# Copy the rest of the project and install it
COPY . .
RUN uv sync --frozen --no-dev

# Stage 2: Lean runtime image
FROM python:3.12-slim

WORKDIR /app

# Copy the venv and app code from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app /app

# Use the venv's Python directly — no uv needed at runtime
ENV PATH="/app/.venv/bin:$PATH"

# Expose ports 8000 (FastAPI) & 8001 (ADK Web UI)
EXPOSE 8000 8001

# Make the startup script executable
RUN chmod +x start.sh

# Run start.sh to launch both processes in parallel & then monitor.
# If either crashes, the script exits and Kubernetes restarts the pod.
CMD ["./start.sh"]

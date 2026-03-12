# Contributing to GitHub Agent & MCP Server for k8s

First off, thank you for considering contributing! This project thrives on community input, whether it's a bug fix, new feature, documentation improvement, or feedback.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How Can I Contribute?](#how-can-i-contribute)
  - [Reporting Bugs](#reporting-bugs)
  - [Suggesting Features](#suggesting-features)
  - [Submitting Pull Requests](#submitting-pull-requests)
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Style Guide](#style-guide)
- [Commit Messages](#commit-messages)
- [Community](#community)

## Code of Conduct

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold it. Please report unacceptable behavior via GitHub Issues.

## How Can I Contribute?

### Reporting Bugs

Before filing a bug, please check existing issues to avoid duplicates. When reporting a bug, include:

- **A clear, descriptive title**
- **Steps to reproduce** the issue
- **Expected vs. actual behavior**
- **Environment details**: OS, Python version, Docker version, Kubernetes version, `kind` version
- **Logs**: Relevant output from `kubectl logs`, FastAPI, or the ADK Web UI
- **Configuration**: Which model you're using (`AGENT_MODEL`), whether you're running locally or in k8s

### Suggesting Features

Feature requests are welcome! Open an issue with the `enhancement` label and describe:

- **The problem** your feature would solve
- **Your proposed solution**
- **Alternatives** you've considered

### Submitting Pull Requests

1. Fork the repository and create your branch from `master`
2. Follow the [Development Setup](#development-setup) below
3. Make your changes and test them locally
4. Ensure your code follows the [Style Guide](#style-guide)
5. Write a clear PR description explaining **what** and **why**
6. Link any related issues (e.g., `Closes #42`)

## Development Setup

### Prerequisites

- Python 3.12+
- Docker
- kind (Kubernetes IN Docker)
- kubectl
- A [GitHub PAT](https://github.com/settings/personal-access-tokens) (for testing)
- A [Google Gemini API key](https://aistudio.google.com/apikey) (or Anthropic API key if using Claude)

### Local Development

```bash
# Clone your fork
git clone https://github.com/alisterbaroi/github-agent.git
cd github-agent

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — fill in GITHUB_PAT and GOOGLE_API_KEY

# Run the FastAPI server locally
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

> **Note:** The agent requires a running GitHub MCP server. For local development, you can run the official GitHub MCP server via Docker:
> ```bash
> docker run -p 8082:8082 \
>   -e GITHUB_PERSONAL_ACCESS_TOKEN=<your-pat> \
>   ghcr.io/github/github-mcp-server:latest http --port 8082
> ```

### Testing in Kubernetes (kind)

Follow the full [Setup & Deployment Guide](README.md#setup--deployment-guide) in the README to spin up a local kind cluster with both the MCP server and agent.

## Project Structure

```
.
├── main.py                              # FastAPI app — A2A endpoints, agent card, health check
├── gh_agent/
│   ├── __init__.py
│   └── agent.py                         # ADK LlmAgent definition + MCP toolset wiring
├── start.sh                             # Container entrypoint (FastAPI + ADK Web UI)
├── Dockerfile                           # Python 3.12-slim image build
├── github-mcp-server-deployment.yaml    # k8s manifest for the GitHub MCP server
├── github-agent-deployment.yaml         # k8s manifest for the agent
├── requirements.txt                     # Pinned Python dependencies
└── .env.example                         # Environment variable template
```

### Key Components

| Component | What it does |
|---|---|
| `gh_agent/agent.py` | Builds the ADK `LlmAgent` with `MCPToolset` connected to the GitHub MCP server |
| `main.py` | Wires the agent into FastAPI via the A2A protocol stack (Runner → A2aAgentExecutor → A2AFastAPIApplication) |
| `start.sh` | Launches both FastAPI (port 8000) and ADK Web UI (port 8001) in parallel inside the container |

## Style Guide

- **Python**: Follow [PEP 8](https://peps.python.org/pep-0008/). Use type hints where practical.
- **Imports**: Group as stdlib → third-party → local, separated by blank lines.
- **Naming**: Use `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- **Kubernetes manifests**: Use `envsubst`-compatible `${VAR}` placeholders — do not hardcode names or namespaces.
- **Secrets**: Never commit API keys, PATs, or credentials. Use environment variables and Kubernetes secrets.
- **Dependencies**: If you add a new package, pin it in `requirements.txt` with the exact version (`package==x.y.z`).

## Commit Messages

Use clear, concise commit messages:

```
<type>: <short summary>

<optional body explaining why>
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`

**Examples:**
```
feat: add /tasks endpoint for listing active A2A tasks
fix: handle missing GITHUB_PAT gracefully on startup
docs: add Vertex AI setup instructions to README
```

## Community

- **Questions?** Open a [Discussion](../../discussions) or an issue tagged `question`
- **Ideas?** We'd love to hear them — open an issue tagged `enhancement`
- **Show & Tell**: Built something cool with this agent? Share it in Discussions!

---

Thank you for helping make this project better!

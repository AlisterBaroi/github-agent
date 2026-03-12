# GitHub Agent & MCP Server for k8s
This project is a headless, FastAPI-based, A2A-native custom agent that dynamically communicates with the official GitHub Model Context Protocol (MCP) server. Both the agent and the MCP server are designed to for self-hosted deployment, side-by-side within a Kubernetes cluster.

## Prerequisites
Before you begin, ensure you have the following installed on your machine:
* [Docker](https://docs.docker.com/get-docker/) — Container runtime
* [kind](https://kind.sigs.k8s.io/) — (Kubernetes IN Docker)
* [kubectl](https://kubernetes.io/docs/tasks/tools/) — Kubernetes command-line tool
* [uv](https://docs.astral.sh/uv/) — (Optional, for local development) Fast Python package manager
* [GitHub Personal Access Token (PAT)](https://github.com/settings/personal-access-tokens) — A fine-grained PAT with read/write access to the repositories you want to manage. Expand [How to Create a GitHub PAT (Expand to read)](#how-to-create-a-github-pat-expand-to-read) section to get started. [Learn more](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

<details>
<summary><h3>How to Create a GitHub PAT (Expand to read)<h3></summary>

> Follow these steps to create a fine-grained GitHub PAT with the required permissions:
>
> 1. **Go to GitHub PAT Settings**
>    - Navigate to [GitHub Settings > Developer settings > Personal access tokens > Fine-grained tokens](https://github.com/settings/personal-access-tokens)
>    - Click **"Generate new token"** (select "Fine-grained token")
> 
> 2. **Configure Token Settings**
>    - **Name**: Enter a descriptive name (e.g., "github-mcp-agent")
>    - **Owner**: Select your account
>    - **Expiration**: Choose your preferred expiration period (e.g., 90 days)
> 
> 3. **Set Repository Access**
>    - Choose **"Only select repositories"** and select the repositories you want to manage
>    - OR choose **"All repositories"** if you want access to all your repos
> 
> 4. **Configure Permissions**
>    Under "Repository permissions", enable:
>    - **Contents**: `Read-only` (and `write` if need file operations)
>    - **Metadata**: `Read-only` (required for repository info)
>    - **Issues**: `Read and write`
>    - **Pull requests**: `Read-only` (and `write`, for optional PR operations)
>    - Set more/less permissions according yo your agent's needs
> 
>    Under "Account permissions", enable:
>    - **Organization administration**: Read only (if working with orgs)
> 
> 5. **Generate and Copy**
>    - Click **"Generate token"**
>    - **Important**: Copy the token immediately - you won't see it again!
>    - Store it securely (use a password manager or Kubernetes secrets)
> 6. **Test Your Token**
>    ```bash
>    curl -H "Authorization: Bearer <YOUR_PAT>" \
>      https://api.github.com/user
>    ```
>    If successful, it will return your user profile JSON.
> 
> 7. **Token Expiration & Rotation**
>   - Set a calendar reminder before your token expires
>   - Create a new token before the old one expires
>   - Update your Kubernetes secret: 
>     ```bash
>     kubectl delete secret github-mcp-secret -n github-mcp && kubectl create secret generic github-mcp-secret --from-literal=GITHUB_PERSONAL_ACCESS_TOKEN=<new-token> -n github-mcp
>     ```
>   - Restart the pods: 
>     ```bash
>     kubectl rollout restart deployment <MCP_SERVER_NAME> -n <NAMESPACE>
>     ```
> </details>

> [!WARNING] 
> Never commit your PAT to git repositories! Always use environment variables or secrets managers.

> [!NOTE]
> Make sure to copy the `.env.example` file, rename as `.env` and paste in the GitHub PAT for the `GITHUB_PAT` value.

## Setup & Deployment Guide
### 1. Set Environment Variables
Set up the necessary environment variables, including your GitHub Personal Access Token (PAT):
```bash
# Cluster Variables
export CLUSTER_NAME="mcp-cluster"
export NAMESPACE="github-mcp"
export DEV_MODE="false" # set true for development

# MCP Server Variables
export MCP_SERVER_NAME="github-mcp-server"
export MCP_SERVICE_NAME="github-mcp-service"
export MCP_SECRET_NAME="github-mcp-secret"

# Agent Variables
export AGENT_NAME="github-custom-agent"
export AGENT_SERVICE_NAME="github-custom-agent-service"
export AGENT_SECRET_NAME="github-agent-secret"
export AGENT_IMAGE="github-agent:latest"

# API Keys & Token
export GITHUB_PAT="<github-access-token-here>"  # << Set github PAT here
export GEMINI_API_KEY="<gemini-api-key-here>"   # << Set gemini api-key here
```

### 2. Create the Cluster & Namespace
Create a new local Kubernetes cluster using `kind` and set up the dedicated namespace:
```bash
kind create cluster --name ${CLUSTER_NAME}
kubectl create namespace ${NAMESPACE}
```

### 3. Deploy the Secret to the Cluster
Securely store your GitHub PAT & Gemini API key to Kubernetes as a Secret so the MCP server & agent can access it:
```bash
kubectl create secret generic ${MCP_SECRET_NAME} \
  --from-literal=GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_PAT} \
  --namespace ${NAMESPACE}

kubectl create secret generic ${AGENT_SECRET_NAME} \
  --from-literal=GOOGLE_API_KEY=${GEMINI_API_KEY} \
  --namespace ${NAMESPACE}
```

### 4. Deploy the GitHub MCP Server
Deploy the official GitHub MCP Server using the provided kubernetes deployment and service manifests. This spins up the server in Streamable HTTP mode on port `8082`.
```bash
envsubst < github-mcp-server-deployment.yaml | kubectl apply -n ${NAMESPACE} -f -
```
<!-- kubectl apply -f github-mcp-server-deployment.yaml -n $NAMESPACE -->

### 5. Build and Deploy the GitHub Agent
Because the agent uses a custom Python image, you need to build it locally and load it into your `kind` cluster before deploying using its kubernetes deployment and service manifests file.
```bash
# Build the Docker image for agent locally (uses uv for fast installs)
docker build -t ${AGENT_IMAGE} .

# Fallback: build with pip if uv is unavailable
# docker build -f Dockerfile.pip -t ${AGENT_IMAGE} .

# Load agent image into the kind cluster
kind load docker-image ${AGENT_IMAGE} --name ${CLUSTER_NAME}

# Deploy the agent image using the manifest
envsubst < github-agent-deployment.yaml | kubectl apply -n ${NAMESPACE} -f -
```
<!-- kubectl apply -f github-agent-deployment.yaml -n $NAMESPACE -->

Wait a few moments and verify that both pods, `github-mcp-server` (`MCP_SERVER_NAME`),  and `github-custom-agent` (`AGENT_NAME`), are running:
```bash
kubectl get pods -n ${NAMESPACE}
```

### 6. Port-Forward for Testing
To interact with your agent from your local machine, open a port-forward tunnel to the agent's Kubernetes service.
**In Terminal 1 (Leave this running):**
```bash
kubectl port-forward service/${AGENT_SERVICE_NAME} 8000:80 -n ${NAMESPACE}
```
<!-- kubectl port-forward service/github-custom-agent-service 8000:80 -n $NAMESPACE -->

**In Terminal 2 (Run your tests):**
Fetch the agent card (discovery):
```bash
curl http://localhost:8000/.well-known/agent.json  
# or use "agent-card.json", instead of "agent.json"
```
Send a task (A2A tasks/send):
```bash
curl -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{
  "id": "test-001",
  "jsonrpc": "2.0",
  "method": "message/send",
  "params": {
    "message": {
      "messageId": "msg-001",
      "role": "user",
      "parts": [{ "kind": "text", "text": "What is the latest open issue on octocat/Hello-World"}]
    }
  }
}'
```

Call from another pod in the same cluster (A2A inter-agent):
```bash
curl -X POST http://${AGENT_SERVICE_NAME}.${NAMESPACE}.svc.cluster.local/ \
  # rest is same as previous ...
```

For viewing API documentations & schema, open: 
- [localhost:8000/docs](http://localhost:8000/docs)
- [localhost:8000/redoc](http://localhost:8000/redoc)

## Cleanup
When you are finished testing, you can tear down the entire `kind` cluster to free up local resources:
```bash
kind delete cluster --name ${CLUSTER_NAME}
```
Optionally, also delete the GitHub PAT from your GitHub settings.

## Local Development
To install dependencies & run locally for development, it's recommended to use the **primary method** using `uv` (fast):
```bash
# install dependencies
uv sync 
# Run development server
uv run uvicorn main:app --reload
```
As a fallback, you can also you the traditional `pip` method:
```bash
# install dependencies
pip install -r requirements.txt
# Run development server
uvicorn main:app --reload
```


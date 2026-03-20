# Deploying GitHub Agent & MCP Server to Google Kubernetes Engine (GKE)

This guide walks through deploying the GitHub Agent and its companion GitHub MCP Server to an existing GKE cluster, using Google Artifact Registry (GAR) for container images and LoadBalancer services with static IPs for external access.

## Prerequisites
* [gcloud CLI](https://cloud.google.com/sdk/docs/install) — authenticated with access to your GCP project
* [kubectl](https://kubernetes.io/docs/tasks/tools/) — Kubernetes command-line tool
* [Docker](https://docs.docker.com/get-docker/) — for building and pushing images
* An existing GKE cluster
* A [GitHub Personal Access Token (PAT)](https://github.com/settings/personal-access-tokens) — see main [README.md](README.md) for setup instructions
* A [Gemini API key](https://aistudio.google.com/apikey)

## 1. Connect to the GKE Cluster

Authenticate with gcloud and get cluster credentials:
```bash
gcloud auth login
gcloud container clusters get-credentials <CLUSTER_NAME> \
  --zone <ZONE> \
  --project <PROJECT_ID>
```

Example:
```bash
gcloud container clusters get-credentials demo-01 \
  --zone europe-west1-b \
  --project tigera-matrix
```

Verify the connection:
```bash
kubectl get nodes
```

## 2. Set Environment Variables

Configure all required variables for the deployment. Adjust values to match your environment:
```bash
# Cluster Variables
export CLUSTER_NAME="demo-01"
export NAMESPACE="github-agent-mcp"
export DEV_MODE="false"  # set "true" to enable ADK Web UI

# MCP Server Variables
export MCP_SERVER_NAME="github-mcp-server"
export MCP_SERVICE_NAME="github-mcp-service"
export MCP_SECRET_NAME="github-mcp-secret"

# Agent Variables
export AGENT_NAME="github-custom-agent"
export AGENT_SERVICE_NAME="github-custom-agent-service"
export AGENT_SECRET_NAME="github-agent-secret"

# GAR image paths
export GAR_REGION="europe-west1"
export GAR_PROJECT="tigera-matrix"
export GAR_REPO="github-agent"
export AGENT_IMAGE="${GAR_REGION}-docker.pkg.dev/${GAR_PROJECT}/${GAR_REPO}/github-agent:latest"
export MCP_SERVER_IMAGE="${GAR_REGION}-docker.pkg.dev/${GAR_PROJECT}/${GAR_REPO}/github-mcp-server:latest"

# API Keys & Tokens
export GITHUB_PAT="<your-github-pat>"
export GEMINI_API_KEY="<your-gemini-api-key>"
```

> [!WARNING]
> Never commit API keys or tokens to version control.

## 3. Create the Artifact Registry Repository

Check for existing repositories:
```bash
gcloud artifacts repositories list --project=${GAR_PROJECT} --location=${GAR_REGION}
```

If no Docker repository exists, create one:
```bash
gcloud artifacts repositories create ${GAR_REPO} \
  --repository-format=docker \
  --location=${GAR_REGION} \
  --project=${GAR_PROJECT} \
  --description="GitHub Agent & MCP Server images"
```

Configure Docker to authenticate with GAR:
```bash
gcloud auth configure-docker ${GAR_REGION}-docker.pkg.dev
```

## 4. Clone the Repository & Build Images

```bash
git clone https://github.com/alisterbaroi/github-agent.git
cd github-agent
git checkout dev-v1.1.0
```

### 4a. Build & Push the Agent Image
```bash
# Build (uses uv for fast dependency installation)
docker build -t ${AGENT_IMAGE} .

# Fallback: build with pip if uv is unavailable
# docker build -f Dockerfile.pip -t ${AGENT_IMAGE} .

# Push to GAR
docker push ${AGENT_IMAGE}
```

### 4b. Pull, Re-tag & Push the MCP Server Image
The MCP server uses the official GitHub MCP Server image. We re-tag and push it to GAR so the cluster pulls from a single registry:
```bash
docker pull ghcr.io/github/github-mcp-server:latest
docker tag ghcr.io/github/github-mcp-server:latest ${MCP_SERVER_IMAGE}
docker push ${MCP_SERVER_IMAGE}
```

## 5. Create Namespace & Secrets

```bash
# Create the namespace
kubectl create namespace ${NAMESPACE}

# Create the MCP server secret (GitHub PAT)
kubectl create secret generic ${MCP_SECRET_NAME} \
  --from-literal=GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_PAT} \
  --namespace ${NAMESPACE}

# Create the agent secret (Gemini API key + dev mode flag)
kubectl create secret generic ${AGENT_SECRET_NAME} \
  --from-literal=GOOGLE_API_KEY=${GEMINI_API_KEY} \
  --from-literal=DEVELOPMENT_MODE=${DEV_MODE} \
  --namespace ${NAMESPACE}
```

## 6. Update Manifests for GAR

Before deploying, update the manifests to use GAR images and GKE-appropriate pull policies:
```bash
# Point MCP server manifest to the GAR image
sed -i 's|image: ghcr.io/github/github-mcp-server:latest|image: '"${MCP_SERVER_IMAGE}"'|' github-mcp-server-deployment.yaml

# Change agent pull policy from IfNotPresent (kind) to Always (GAR)
sed -i 's|imagePullPolicy: IfNotPresent.*|imagePullPolicy: Always|' github-agent-deployment.yaml
```

## 7. Deploy to GKE

```bash
# Deploy the MCP Server
envsubst < github-mcp-server-deployment.yaml | kubectl apply -n ${NAMESPACE} -f -

# Deploy the Agent
envsubst < github-agent-deployment.yaml | kubectl apply -n ${NAMESPACE} -f -
```

Wait for both pods to be running:
```bash
kubectl get pods -n ${NAMESPACE} -w
```

Expected output:
```
NAME                                  READY   STATUS    RESTARTS   AGE
github-custom-agent-d4bccc5f8-xxxxx   1/1     Running   0          40s
github-mcp-server-6fd8dbc4f6-xxxxx    1/1     Running   0          41s
```

Press `Ctrl+C` once both show `Running` with `1/1` ready.

## 8. Expose via LoadBalancer

### Option A: Static IP (Recommended for Production)

Reserve a static IP for the agent:
```bash
gcloud compute addresses create github-agent-ip \
  --region=${GAR_REGION} \
  --project=${GAR_PROJECT}

# Get the reserved IP
gcloud compute addresses describe github-agent-ip \
  --region=${GAR_REGION} \
  --project=${GAR_PROJECT} --format='get(address)'
```

Patch the agent service to use the static IP:
```bash
kubectl patch service ${AGENT_SERVICE_NAME} -n ${NAMESPACE} \
  -p '{"spec": {"type": "LoadBalancer", "loadBalancerIP": "<STATIC_IP>"}}'
```

Verify the external IP is assigned:
```bash
kubectl get service ${AGENT_SERVICE_NAME} -n ${NAMESPACE} -w
```

If `DEV_MODE` is `true` and you also want to expose the ADK Web UI:
```bash
# Reserve a second static IP
gcloud compute addresses create github-agent-adk-ip \
  --region=${GAR_REGION} \
  --project=${GAR_PROJECT}

gcloud compute addresses describe github-agent-adk-ip \
  --region=${GAR_REGION} \
  --project=${GAR_PROJECT} --format='get(address)'

# Patch the ADK service
kubectl patch service ${AGENT_SERVICE_NAME}-adk -n ${NAMESPACE} \
  -p '{"spec": {"type": "LoadBalancer", "loadBalancerIP": "<ADK_STATIC_IP>"}}'

kubectl get service ${AGENT_SERVICE_NAME}-adk -n ${NAMESPACE} -w
```

### Option B: Dynamic IP (Simpler, Non-Production)

If you don't need a fixed IP, simply change the service type — GKE will assign an ephemeral external IP automatically:
```bash
kubectl patch service ${AGENT_SERVICE_NAME} -n ${NAMESPACE} \
  -p '{"spec": {"type": "LoadBalancer"}}'
```

Check the assigned IP:
```bash
kubectl get service ${AGENT_SERVICE_NAME} -n ${NAMESPACE}
```

> [!NOTE]
> Dynamic IPs may change if the service is recreated. Use static IPs for stable, production endpoints.

## 9. Testing

Replace `<EXTERNAL_IP>` with your agent's LoadBalancer IP.

### Health & Probes
```bash
# Liveness probe
curl http://<EXTERNAL_IP>/healthz

# Readiness probe (deep dependency check)
curl http://<EXTERNAL_IP>/readyz

# MCP tool catalogue
curl http://<EXTERNAL_IP>/list_all_tools
```

### A2A JSON-RPC
```bash
# Agent card discovery
curl http://<EXTERNAL_IP>/.well-known/agent.json

# Send a task (message/send)
curl -X POST http://<EXTERNAL_IP>/ \
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

### Simple Message Endpoint
```bash
curl -X POST http://<EXTERNAL_IP>/message \
  -H "Content-Type: application/json" \
  -d '{"message": "List all open issues in octocat/Hello-World"}'
```

### Inter-Agent Communication (from within the cluster)
```bash
curl -X POST http://${AGENT_SERVICE_NAME}.${NAMESPACE}.svc.cluster.local/ \
  -H "Content-Type: application/json" \
  -d '{
  "id": "internal-001",
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

### API Documentation
- Swagger UI: `http://<EXTERNAL_IP>/docs`
- ReDoc: `http://<EXTERNAL_IP>/redoc`
- ADK Web UI (if `DEV_MODE=true`): `http://<ADK_EXTERNAL_IP>:8001`

## 10. Enabling / Disabling DEV_MODE

`DEV_MODE` controls whether the ADK Web UI runs alongside the FastAPI server on port 8001. You can toggle it without rebuilding the image or redeploying the manifest.

### Enable DEV_MODE
```bash
kubectl get secret ${AGENT_SECRET_NAME} -n ${NAMESPACE} -o json \
  | jq --arg val "$(echo -n 'true' | base64)" '.data.DEVELOPMENT_MODE = $val' \
  | kubectl apply -f -

# Restart the agent to pick up the change
kubectl rollout restart deployment/${AGENT_NAME} -n ${NAMESPACE}
kubectl rollout status deployment/${AGENT_NAME} -n ${NAMESPACE}
```

### Disable DEV_MODE
```bash
kubectl get secret ${AGENT_SECRET_NAME} -n ${NAMESPACE} -o json \
  | jq --arg val "$(echo -n 'false' | base64)" '.data.DEVELOPMENT_MODE = $val' \
  | kubectl apply -f -

# Restart the agent to pick up the change
kubectl rollout restart deployment/${AGENT_NAME} -n ${NAMESPACE}
kubectl rollout status deployment/${AGENT_NAME} -n ${NAMESPACE}
```

### Verify Current DEV_MODE Value
```bash
kubectl get secret ${AGENT_SECRET_NAME} -n ${NAMESPACE} \
  -o jsonpath='{.data.DEVELOPMENT_MODE}' | base64 -d && echo
```

## Cleanup

To remove all deployed resources:
```bash
# Delete the deployments and services
kubectl delete namespace ${NAMESPACE}

# Release static IPs (optional)
gcloud compute addresses delete github-agent-ip \
  --region=${GAR_REGION} --project=${GAR_PROJECT} --quiet
gcloud compute addresses delete github-agent-adk-ip \
  --region=${GAR_REGION} --project=${GAR_PROJECT} --quiet

# Delete GAR images (optional)
gcloud artifacts docker images delete ${AGENT_IMAGE} --quiet
gcloud artifacts docker images delete ${MCP_SERVER_IMAGE} --quiet

# Delete the GAR repository (optional)
gcloud artifacts repositories delete ${GAR_REPO} \
  --location=${GAR_REGION} --project=${GAR_PROJECT} --quiet
```

## Redeployment (Image Update)

To redeploy after code changes:
```bash
# Rebuild and push the agent image
docker build -t ${AGENT_IMAGE} .
docker push ${AGENT_IMAGE}

# Restart the deployment to pull the new image
kubectl rollout restart deployment/${AGENT_NAME} -n ${NAMESPACE}
kubectl rollout status deployment/${AGENT_NAME} -n ${NAMESPACE}
```

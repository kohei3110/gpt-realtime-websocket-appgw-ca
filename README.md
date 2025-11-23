# GPT Realtime WebSocket via Application Gateway

English documentation is below. 日本語ドキュメントは[README.ja.md](README.ja.md)を参照してください。

## Overview

This repository provisions a minimal Azure workload for validating WebSocket blue/green behavior when Azure Application Gateway fronts Azure Container Apps, which proxies client WebSockets to Azure OpenAI `gpt-realtime`.

Core validation themes:

- **cooldownPeriod** – ensure scale down candidates do not receive new traffic while existing connections drain
- **session affinity** – confirm long-lived client connections stay bound to the original revision
- **graceful termination** – observe SIGTERM/SIGKILL timing inside Container Apps (terminationGracePeriodSeconds = 30)
- **multi-revision routing** – verify weighted traffic split keeps existing sessions alive while shifting new ones

## Architecture

```
Client WebSocket --> Application Gateway (HTTP listener) --> Azure Container Apps (FastAPI)
                                                       \-> Azure OpenAI gpt-realtime (WebSocket)
Azure Container Registry stores FastAPI images used by ACA revisions.
```

Key files:

- `infra/main.bicep` – deploys ACR, Azure OpenAI, Container Apps, Application Gateway
- `src/main.py` – FastAPI WebSocket proxy (text-only) with stdout logging
- `tests/websocket_client.py` – holds long-running WebSocket sessions (5 minutes default)
- `scripts/test-blue-green.sh` – automates build, push, new revision rollout, and traffic shifts

## Prerequisites

- Azure CLI `>= 2.63`
- Bicep CLI `>= 0.27` (or `az bicep install`)
- Docker 24+
- Python 3.11 (for local testing)
- Azure subscription with Azure OpenAI preview access (gpt-realtime)

Environment variables you will need during deployment:

- `RESOURCE_GROUP`
- `LOCATION`
- `PREFIX` (3+ characters)
- `AZURE_OPENAI_DEPLOYMENT` (e.g. `gpt-realtime`)

## Deploy Infrastructure

```bash
# 1. Create a resource group
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION"

# 2. Deploy all infrastructure
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file infra/main.bicep \
  --parameters prefix="$PREFIX" \
               openAiDeploymentName="$AZURE_OPENAI_DEPLOYMENT" \
               openAiDeploymentSku="GlobalStandard" \
               openAiModelName="gpt-realtime-preview" \
               openAiModelVersion="2024-08-06"
```

Outputs include `containerRegistryLoginServer`, `containerAppName`, and the Application Gateway public IP.

> **Note**: `openAiDeploymentSku` defaults to `GlobalStandard`, which is required for `gpt-realtime`. Override only if Microsoft introduces additional realtime SKUs.

## Build and Push the FastAPI Image

```bash
ACR_LOGIN_SERVER=$(az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name <deployment-name> \
  --query "properties.outputs.containerRegistryLoginServer.value" -o tsv)

IMAGE_TAG=v0.1.0
IMAGE_NAME="$ACR_LOGIN_SERVER/$PREFIX-ws:$IMAGE_TAG"

az acr login --name "$ACR_LOGIN_SERVER"
docker build -t "$IMAGE_NAME" .
docker push "$IMAGE_NAME"
```

Update the Container App to use your image:

```bash
az containerapp update \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --image "$IMAGE_NAME" \
  --revision-suffix "rev$(date +%H%M%S)"
```

Set the required secrets (Azure OpenAI key) if you did not supply it via Bicep parameters:

```bash
az containerapp secret set \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --secrets azure-openai-api-key="$AZURE_OPENAI_KEY"
```

Bind the secret to the runtime environment variable so the proxy can read `AZURE_OPENAI_API_KEY`:

```bash
az containerapp update \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --set-env-vars AZURE_OPENAI_API_KEY=secretref:azure-openai-api-key
```

> When you pass `openAiApiKey` to `infra/main.bicep`, the template performs both steps (secret creation + env var binding) automatically. Only run the CLI commands above if you skipped that parameter during deployment.

## Local Testing

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn src.main:app --host 0.0.0.0 --port 8080
```

Use `tests/websocket_client.py` to hold sessions locally against `ws://localhost:8080/ws`.

## WebSocket Behavior

The FastAPI proxy exposes `/ws` and bridges events to Azure OpenAI Realtime over
`oai.realtime.v1`. Only the minimum client events documented in the
[Realtime Audio reference](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/realtime-audio-reference?view=foundry-classic#client-events)
are accepted:

- `session.update`
- `conversation.item.create` (content must be `input_text`)
- `conversation.item.delete`
- `conversation.item.truncate`
- `response.create`
- `response.cancel`

Any other event types are rejected with a simple error payload. Server events
from Azure are streamed back to the caller unchanged, so `tests/websocket_client.py`
can be used against either `ws://localhost:8080/ws` or the Application Gateway
endpoint to observe blue/green rollouts.

## Environment Configuration

`.env.example` lists the variables the FastAPI proxy expects when running outside Container Apps:

```bash
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-realtime
AZURE_OPENAI_API_KEY=<api-key>
AZURE_OPENAI_API_VERSION=2025-04-01-preview
PORT=8080
CONTAINER_APP_REVISION=local
```

With those variables defined, the proxy connects to Azure via `wss://<endpoint>/openai/v1?api-version=<version>&model=<deployment>`, which matches the latest Realtime quickstart guidance. If your resource still exposes a `*.cognitiveservices.azure.com` host, the proxy automatically falls back to the legacy `.../openai/realtime?deployment=` format so existing GlobalStandard deployments continue to work.

Copy it to `.env` (git-ignored) and fill in real values for local smoke tests.

## GitHub Actions Deployment

This repo ships with `.github/workflows/deploy.yml`, which builds a container image, pushes it to Azure Container Registry, and updates your Container App. Authentication is handled by the [Configure Azure Settings](https://github.com/marketplace/configure-azure-settings) GitHub App, so no `azure/login` step is required.

1. Install the Configure Azure Settings app on this repository and link it to your subscription/resource group.
2. Define repository **variables** (Settings → Secrets and Variables → Actions → Variables):
  - `AZURE_CONTAINER_REGISTRY` (e.g., `gptrtacr`)
  - `RESOURCE_GROUP` (e.g., `rg-gptrealtimewebsocket-demo-swedencentral-001`)
  - `CONTAINER_APP_NAME` (e.g., `gptrt-ws`)
3. Push to `main` (or run the workflow manually via *Run workflow*). The pipeline will:
  - `az acr login`
  - `docker build` and `docker push`
  - `az containerapp update` with a new revision suffix
  - Emit the Container App ingress FQDN for quick verification

## Observe Logs (SIGTERM/SIGKILL)

Container Apps streams stdout/stderr to Log Analytics. Tail in real time:

```bash
az containerapp logs show \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --follow
```

Look for records such as `signal.received`, `client.connected`, `bridge.completed`, and `connection.closed` to understand lifecycle timing.

## Blue-Green Workflow Helper

`scripts/test-blue-green.sh` automates a simple rollout:

```bash
export RESOURCE_GROUP=<rg>
export CONTAINERAPP_NAME=$PREFIX-ws
export ACR_NAME=<acr-name>
export IMAGE_TAG=v0.1.1

./scripts/test-blue-green.sh
```

The script performs:

1. `docker build` + `docker push`
2. `az containerapp update` with a new revision suffix
3. Weighted traffic split (50/50, then 100% to new revision)
4. Deactivation of the previous revision
5. Emits `az containerapp logs show` command so you can watch SIGTERM timing

## Long-Running Client Harness

Keep five connections alive for five minutes to observe routing during the rollout:

```bash
python tests/websocket_client.py \
  "ws://<application-gateway-ip>/ws" \
  --connections 5 \
  --duration 300 \
  --ping-interval 30
```

## Next Steps

- Add TLS certificates to Application Gateway
- Integrate Application Insights for deeper telemetry
- Extend the FastAPI proxy to emit structured logs into Azure Monitor tables

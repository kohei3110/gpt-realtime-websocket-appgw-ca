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
               openAiModelName="gpt-realtime" \
               openAiModelVersion="2025-08-28"
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
  --secrets azure-openai-api-key="$AZURE_OPENAI_API_KEY"
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

Use `tests/websocket_client.py` to hold sessions locally against `ws://localhost:8080/chat`.

## WebSocket Behavior

The FastAPI proxy exposes `/chat` and accepts simple JSON messages with a `text` field.
It then relays the user's text to Azure OpenAI Realtime API and streams back:

- Text responses (`response.text.delta`)
- Audio responses (`response.audio.delta`)
- Audio transcripts (`response.audio_transcript.delta`)
- Status messages

The web interface at the root path (`/`) provides a browser-based client that
connects to the WebSocket endpoint and displays both text and plays audio responses.
`tests/websocket_client.py` can be used against either `ws://localhost:8080/chat`
or the Application Gateway endpoint to observe blue/green rollouts.

## Environment Configuration

`.env.example` lists the variables the FastAPI proxy expects when running outside Container Apps:

```bash
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-realtime
AZURE_OPENAI_API_KEY=<api-key>
AZURE_OPENAI_API_VERSION=2025-08-28
PORT=8080
CONTAINER_APP_REVISION=local
```

With those variables defined, the proxy connects to Azure via `wss://<endpoint>/openai/v1?api-version=<version>&model=<deployment>`, which matches the latest Realtime quickstart guidance. If your resource still exposes a `*.cognitiveservices.azure.com` host, the proxy automatically falls back to the legacy `.../openai/realtime?deployment=` format so existing GlobalStandard deployments continue to work.

Copy it to `.env` (git-ignored) and fill in real values for local smoke tests.

## Application Gateway WebSocket Connection

By default, the FastAPI application dynamically resolves the WebSocket endpoint based on the current host (browser's `location.host`). This means:

- When accessed via Container Apps directly: `ws://<container-app-fqdn>/chat`
- When accessed via Application Gateway: `ws://<agw-public-ip>/chat`

### Using Environment Variable (Optional)

If you need to explicitly configure the Application Gateway endpoint, set the `APPLICATION_GATEWAY_HOST` environment variable:

```bash
# Get Application Gateway Public IP
AGW_IP=$(az network public-ip show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$PREFIX-pip" \
  --query ipAddress -o tsv)

# Update Container App with Application Gateway host
az containerapp update \
  --name "$PREFIX-ws" \
  --resource-group "$RESOURCE_GROUP" \
  --set-env-vars APPLICATION_GATEWAY_HOST="$AGW_IP"
```

When `APPLICATION_GATEWAY_HOST` is set, the application will serve HTML that connects to:
- `ws://<APPLICATION_GATEWAY_HOST>/chat` (for HTTP)
- `wss://<APPLICATION_GATEWAY_HOST>/chat` (for HTTPS)

### Testing WebSocket Connection

```bash
# Get Application Gateway IP
AGW_IP=$(az deployment group show \
  --resource-group "$RESOURCE_GROUP" \
  --name <deployment-name> \
  --query "properties.outputs.applicationGatewayPublicIp.value" -o tsv)

# Test with the WebSocket client
python tests/websocket_client.py "ws://$AGW_IP/chat" \
  --connections 5 \
  --duration 300 \
  --ping-interval 30
```

Or open your browser to `http://$AGW_IP/` to test the web interface.

### Application Gateway Configuration Notes

The Application Gateway is configured to support WebSocket connections:

- **Backend Protocol**: HTTP (port 80) - TLS termination happens at Application Gateway
- **Request Timeout**: 120 seconds (adjust for longer WebSocket sessions if needed)
- **Backend Pool**: Points to Container Apps ingress FQDN
- **Health Probe**: Checks `/healthz` endpoint every 30 seconds

For production deployments, consider:
- Adding TLS/SSL certificate to Application Gateway frontend
- Increasing `requestTimeout` for long-lived WebSocket connections
- Configuring `connectionDraining` for graceful shutdown

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
  "ws://<application-gateway-ip>/chat" \
  --connections 5 \
  --duration 300 \
  --ping-interval 30
```

## Next Steps

- Add TLS certificates to Application Gateway
- Integrate Application Insights for deeper telemetry
- Extend the FastAPI proxy to emit structured logs into Azure Monitor tables

## Sideband Architecture Demo (WebRTC + WebSocket Session Separation)

This repository includes a demonstration of OpenAI's [sideband server controls](https://platform.openai.com/docs/guides/realtime-server-controls) approach, which separates the user-OpenAI connection from the server-OpenAI control channel.

**Now supports both Azure OpenAI and OpenAI Direct API!**

### Architecture

```
┌─────────────┐                      ┌─────────────────────┐
│    User     │◄─────WebRTC─────────►│  Azure OpenAI /     │
│  (Browser)  │   (audio/video)      │  OpenAI Realtime    │
└─────────────┘                      └─────────────────────┘
                                              ▲
                                              │
                                         WebSocket
                                        (call_id)
                                              │
                                        ┌─────┴─────┐
                                        │   Server  │
                                        │ (Control) │
                                        └───────────┘
```

### Benefits

- **Low Latency**: Audio streams directly between user and OpenAI via WebRTC, bypassing the server
- **Scalability**: Server does not need to handle audio data, reducing bandwidth and CPU requirements
- **Separation of Concerns**: Server handles business logic, tools, and session management while media flows independently
- **Cost Efficiency**: Less server resources needed for audio processing

### How It Works

1. **Session Creation**: Server creates a session ID for tracking
2. **WebRTC Connection**: User establishes direct WebRTC connection to OpenAI, receiving a `call_id`
3. **Sideband Connection**: Server connects to the SAME OpenAI session using the `call_id` via WebSocket
4. **Parallel Operation**: Both connections share the session - user sends audio, server monitors and controls

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sideband` | GET | Web UI for sideband demo |
| `/sideband/config` | GET | Get current provider configuration |
| `/sideband/session` | POST | Create a new sideband session |
| `/sideband/ephemeral-key` | POST | Get ephemeral key for WebRTC |
| `/sideband/offer` | POST | Exchange WebRTC SDP offer |
| `/sideband/control/{session_id}` | WS | Server sideband control WebSocket |
| `/sideband/sessions` | GET | List all active sessions |
| `/sideband/session/{session_id}` | GET | Get session details |

### Testing with Azure OpenAI

1. **Environment Setup**: Set Azure OpenAI environment variables

```bash
# Required for Azure OpenAI
export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
export AZURE_OPENAI_API_KEY=your-api-key
export AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o-realtime-preview

# Supported models: gpt-4o-realtime-preview, gpt-4o-mini-realtime-preview, gpt-realtime, gpt-realtime-mini
# Supported regions: East US 2, Sweden Central
```

2. **Start the Server**:

```bash
python -m uvicorn src.main:app --host 0.0.0.0 --port 8080
```

3. **Access the Demo**: Open `http://localhost:8080/sideband` in your browser

4. **Follow the Steps**:
   - Click "1. Create Session" to initialize a sideband session
   - Click "2. Connect WebRTC" to establish direct audio connection to Azure OpenAI
   - Click "3. Connect Server Sideband" to connect the server control channel
   - Use "Start Microphone" to begin audio streaming
   - Use "Update Instructions" or "Send Server Message" to demonstrate server-side control

### Testing with OpenAI Direct API

If you don't have Azure OpenAI, you can use OpenAI's direct API:

```bash
# For OpenAI direct API (without AZURE_OPENAI_ENDPOINT set)
export OPENAI_API_KEY=sk-your-api-key
export OPENAI_REALTIME_MODEL=gpt-4o-realtime-preview-2024-12-17
```

### Understanding the Logs

When running the demo, observe the server logs for session separation confirmation:

```
============================================================
[SIDEBAND SESSION LOG] 2025-11-30T10:30:15.123456
  Provider: AZURE
  Session ID: sideband_abc123def456
  Call ID: rtc_u1_9c6574da8b8a41a18da9308f4ad974ce
  Event: Server WebSocket Connected
  Details: Now BOTH user (WebRTC) and server (WebSocket) are connected to the SAME Azure OpenAI session!
  WebRTC Connected: True
  WebSocket (Server) Connected: True
  Events from OpenAI: 5
  Events to OpenAI: 2
============================================================
```

Key log messages to look for:
- `[NEW SIDEBAND SESSION CREATED]` - Session initialized
- `[WEBRTC CONNECTION ESTABLISHED]` - User connected via WebRTC, `call_id` obtained
- `[SERVER SIDEBAND CONNECTION STARTING]` - Server connecting to same session
- `[SIDEBAND SESSION LOG]` - Ongoing session status with both connections active

### Key Implementation Files

- [src/sideband.py](src/sideband.py) - Sideband module with Azure OpenAI and OpenAI support
- [src/main.py](src/main.py) - Main FastAPI app integrating sideband endpoints

### Azure OpenAI Specific Notes

- **API Endpoints**:
  - Ephemeral key: `https://{resource}.openai.azure.com/openai/v1/realtime/client_secrets`
  - WebRTC calls: `https://{resource}.openai.azure.com/openai/v1/realtime/calls`
  - WebSocket sideband: `wss://{resource}.openai.azure.com/openai/v1/realtime?call_id={call_id}`
- **Authentication**: Uses API key (`api-key` header) or Azure AD Bearer token
- **Supported Regions**: East US 2, Sweden Central (GlobalStandard deployments)

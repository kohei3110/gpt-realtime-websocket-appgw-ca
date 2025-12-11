"""
Sideband Architecture Implementation for OpenAI Realtime API

This module implements the sideband approach where:
- User <-> OpenAI: Direct WebRTC connection (audio/video streaming)
- Server <-> OpenAI: WebSocket connection using call_id (session monitoring & control)

This separation allows:
- Audio data to flow directly between user and OpenAI (low latency)
- Server to handle business logic, tools, and session management
- Scalable architecture where server is not bottleneck for media streams

Supports both:
- Azure OpenAI (AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY)
- OpenAI Direct API (OPENAI_API_KEY)
"""

import asyncio
import contextlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel


# Session tracking for demonstrating session separation
@dataclass
class SidebandSession:
    """Tracks a sideband session with both WebRTC and WebSocket connections."""

    session_id: str
    call_id: str  # OpenAI call_id from WebRTC SDP response
    created_at: datetime
    webrtc_connected: bool = False
    websocket_connected: bool = False
    user_agent: str = ""
    events_from_openai: int = 0
    events_to_openai: int = 0
    last_event_type: str = ""
    last_activity: datetime = field(default_factory=datetime.now)
    provider: str = ""  # "azure" or "openai"


class EphemeralKeyRequest(BaseModel):
    """Request for ephemeral key generation."""

    voice: str = "alloy"
    instructions: str = "You are a helpful assistant."


class WebRTCOfferRequest(BaseModel):
    """Request for WebRTC offer exchange."""

    sdp: str
    session_id: str


# In-memory session store (for demonstration)
_sessions: dict[str, SidebandSession] = {}
_websocket_connections: dict[str, WebSocket] = {}


def _is_azure_openai() -> bool:
    """Check if Azure OpenAI is configured."""
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT"))


def _get_azure_resource() -> str:
    """Extract Azure resource name from endpoint."""
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    # https://myresource.openai.azure.com/ -> myresource
    if endpoint:
        endpoint = endpoint.rstrip("/")
        if ".openai.azure.com" in endpoint:
            return endpoint.replace("https://", "").replace(".openai.azure.com", "")
    return ""


def _get_api_key() -> str:
    """Get API key from environment."""
    if _is_azure_openai():
        key = os.getenv("AZURE_OPENAI_API_KEY")
        if not key:
            raise RuntimeError("AZURE_OPENAI_API_KEY is required for Azure OpenAI")
        return key
    else:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is required")
        return key


def _get_base_url() -> str:
    """Get base URL for API calls."""
    if _is_azure_openai():
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
        return f"{endpoint}/openai"
    else:
        return "https://api.openai.com"


def _get_model() -> str:
    """Get the realtime model/deployment to use."""
    if _is_azure_openai():
        # Support both AZURE_OPENAI_DEPLOYMENT_NAME and legacy AZURE_OPENAI_DEPLOYMENT
        return os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME") or os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-realtime-preview")
    else:
        return os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")


def _get_auth_headers() -> dict[str, str]:
    """Get authentication headers based on provider."""
    if _is_azure_openai():
        return {"api-key": _get_api_key()}
    else:
        return {"Authorization": f"Bearer {_get_api_key()}"}


def _log_session_info(session: SidebandSession, event: str, details: str = "") -> None:
    """Log session information to demonstrate session separation."""
    timestamp = datetime.now().isoformat()
    separator = "=" * 60
    print(f"\n{separator}")
    print(f"[SIDEBAND SESSION LOG] {timestamp}")
    print(f"  Provider: {session.provider.upper()}")
    print(f"  Session ID: {session.session_id}")
    print(f"  Call ID: {session.call_id}")
    print(f"  Event: {event}")
    if details:
        print(f"  Details: {details}")
    print(f"  WebRTC Connected: {session.webrtc_connected}")
    print(f"  WebSocket (Server) Connected: {session.websocket_connected}")
    print(f"  Events from OpenAI: {session.events_from_openai}")
    print(f"  Events to OpenAI: {session.events_to_openai}")
    print(f"{separator}\n")


def create_sideband_app() -> FastAPI:
    """Create FastAPI app with sideband endpoints."""

    app = FastAPI(
        title="OpenAI Realtime Sideband Demo",
        description="Demonstrates session separation between WebRTC (user) and WebSocket (server)",
    )

    @app.get("/sideband", response_class=HTMLResponse)
    async def sideband_index() -> HTMLResponse:
        """Serve the sideband demo page."""
        # Pass configuration to HTML
        is_azure = _is_azure_openai()
        azure_resource = _get_azure_resource() if is_azure else ""
        html = SIDEBAND_HTML.replace("{{IS_AZURE}}", str(is_azure).lower())
        html = html.replace("{{AZURE_RESOURCE}}", azure_resource)
        return HTMLResponse(html)

    @app.get("/sideband/config")
    async def get_config() -> JSONResponse:
        """Get sideband configuration (which provider is being used)."""
        is_azure = _is_azure_openai()
        return JSONResponse(
            {
                "provider": "azure" if is_azure else "openai",
                "azure_resource": _get_azure_resource() if is_azure else None,
                "model": _get_model(),
                "base_url": _get_base_url(),
            }
        )

    @app.post("/sideband/session")
    async def create_session() -> JSONResponse:
        """
        Create a new sideband session.
        Returns session_id for the client to use.
        """
        session_id = f"sideband_{secrets.token_hex(8)}"
        provider = "azure" if _is_azure_openai() else "openai"
        session = SidebandSession(
            session_id=session_id,
            call_id="",  # Will be set after WebRTC connection
            created_at=datetime.now(),
            provider=provider,
        )
        _sessions[session_id] = session

        print(f"\n{'#' * 60}")
        print(f"[NEW SIDEBAND SESSION CREATED]")
        print(f"  Provider: {provider.upper()}")
        print(f"  Session ID: {session_id}")
        print(f"  Created at: {session.created_at.isoformat()}")
        print(f"  Purpose: This session will have TWO separate connections to OpenAI:")
        print(f"    1. WebRTC: User <-> OpenAI (direct audio/video)")
        print(f"    2. WebSocket: Server <-> OpenAI (control channel)")
        print(f"{'#' * 60}\n")

        return JSONResponse(
            {
                "session_id": session_id,
                "provider": provider,
                "message": "Session created. Next: exchange WebRTC offer to get call_id",
            }
        )

    @app.post("/sideband/ephemeral-key")
    async def get_ephemeral_key(request: EphemeralKeyRequest) -> JSONResponse:
        """
        Get an ephemeral key for WebRTC connection.
        This key is used by the client to establish WebRTC connection with OpenAI.

        For Azure OpenAI: POST to /openai/v1/realtime/client_secrets
        For OpenAI: POST to /v1/realtime/sessions
        """
        model = _get_model()
        is_azure = _is_azure_openai()

        print(f"\n[EPHEMERAL KEY REQUEST]")
        print(f"  Provider: {'Azure OpenAI' if is_azure else 'OpenAI'}")
        print(f"  Model/Deployment: {model}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            if is_azure:
                # Azure OpenAI endpoint
                url = f"{_get_base_url()}/v1/realtime/client_secrets"
                headers = {
                    **_get_auth_headers(),
                    "Content-Type": "application/json",
                }
                payload = {
                    "session": {
                        "type": "realtime",
                        "model": model,
                        "instructions": request.instructions,
                        "audio": {
                            "output": {
                                "voice": request.voice,
                            },
                        },
                    },
                }
            else:
                # OpenAI direct endpoint
                url = f"{_get_base_url()}/v1/realtime/sessions"
                headers = {
                    **_get_auth_headers(),
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": model,
                    "voice": request.voice,
                    "instructions": request.instructions,
                    "modalities": ["text", "audio"],
                    "input_audio_transcription": {"model": "whisper-1"},
                }

            print(f"  URL: {url}")
            print(f"  Payload: {payload}")
            response = await client.post(url, headers=headers, json=payload)

            if response.status_code != 200:
                error_detail = response.text
                print(f"  ERROR: Failed to get ephemeral key: {error_detail}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to get ephemeral key: {error_detail}",
                )

            data = response.json()

            # Azure returns token in 'value', OpenAI returns in 'client_secret.value'
            if is_azure:
                token = data.get("value", "")
                print(f"  SUCCESS: Ephemeral key obtained from Azure OpenAI")
            else:
                token = data.get("client_secret", {}).get("value", "")
                print(f"  SUCCESS: Ephemeral key obtained from OpenAI")
                print(f"  Expires at: {data.get('expires_at', 'unknown')}")

            return JSONResponse(
                {
                    "token": token,
                    "provider": "azure" if is_azure else "openai",
                    "raw_response": data,
                }
            )

    @app.post("/sideband/offer")
    async def exchange_offer(request: WebRTCOfferRequest) -> JSONResponse:
        """
        Exchange WebRTC SDP offer with OpenAI.
        Returns SDP answer and call_id for sideband connection.

        The call_id is crucial - it allows the server to connect to the SAME
        OpenAI session via WebSocket while the user connects via WebRTC.

        For Azure OpenAI: POST to /openai/v1/realtime/calls
        For OpenAI: POST to /v1/realtime/calls
        """
        session = _sessions.get(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        model = _get_model()
        is_azure = _is_azure_openai()

        print(f"\n[WEBRTC OFFER EXCHANGE]")
        print(f"  Provider: {'Azure OpenAI' if is_azure else 'OpenAI'}")
        print(f"  Session ID: {request.session_id}")
        print(f"  Exchanging SDP offer...")

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Get ephemeral key
            if is_azure:
                # Put API version 2025-08-28
                key_url = f"{_get_base_url()}/v1/realtime/client_secrets"
                key_headers = {
                    **_get_auth_headers(),
                    "Content-Type": "application/json",
                }
                key_payload = {
                    "session": {
                        "type": "realtime",
                        "model": model,
                        # "model": "gpt-realtime-shkinosh",
                        "instructions": "You are a helpful assistant.",
                        "audio": {"output": {"voice": "alloy"}},
                    },
                }
            else:
                key_url = f"{_get_base_url()}/v1/realtime/sessions"
                key_headers = {
                    **_get_auth_headers(),
                    "Content-Type": "application/json",
                }
                key_payload = {
                    "model": model,
                    "voice": "alloy",
                    "modalities": ["text", "audio"],
                }

            print(f"  Step 1: Getting ephemeral key from {key_url}")
            print(f"  payload: {key_payload}")
            key_response = await client.post(
                key_url, headers=key_headers, json=key_payload
            )

            if key_response.status_code != 200:
                raise HTTPException(
                    status_code=key_response.status_code,
                    detail=f"Failed to get ephemeral key: {key_response.text}",
                )

            key_data = key_response.json()
            if is_azure:
                ephemeral_key = key_data.get("value", "")
            else:
                ephemeral_key = key_data.get("client_secret", {}).get("value", "")

            if not ephemeral_key:
                raise HTTPException(status_code=500, detail="No ephemeral key in response")

            print(f"  Step 1 SUCCESS: Got ephemeral key")

            # Step 2: Exchange SDP offer
            if is_azure:
                sdp_url = f"{_get_base_url()}/v1/realtime/calls"
            else:
                sdp_url = f"{_get_base_url()}/v1/realtime/calls"

            sdp_headers = {
                "Authorization": f"Bearer {ephemeral_key}",
                "Content-Type": "application/sdp",
            }

            print(f"  Step 2: Exchanging SDP at {sdp_url}")
            sdp_response = await client.post(
                sdp_url, headers=sdp_headers, content=request.sdp
            )

            if sdp_response.status_code != 201:
                raise HTTPException(
                    status_code=sdp_response.status_code,
                    detail=f"Failed to exchange SDP: {sdp_response.text}",
                )

            # Extract call_id from Location header
            location = sdp_response.headers.get("Location", "")
            call_id = location.split("/")[-1] if location else ""

            if not call_id:
                raise HTTPException(status_code=500, detail="No call_id in response")

            # Update session with call_id
            session.call_id = call_id
            session.webrtc_connected = True
            session.provider = "azure" if is_azure else "openai"

            print(f"\n{'*' * 60}")
            print(f"[WEBRTC CONNECTION ESTABLISHED]")
            print(f"  Provider: {session.provider.upper()}")
            print(f"  Session ID: {session.session_id}")
            print(f"  Call ID: {call_id}")
            print(f"  Location Header: {location}")
            print(f"  This call_id allows server to connect to the SAME OpenAI session!")
            print(f"  User audio flows directly: User <-> OpenAI via WebRTC")
            print(f"{'*' * 60}\n")

            _log_session_info(session, "WebRTC Connected", f"call_id: {call_id}")

            return JSONResponse(
                {
                    "sdp": sdp_response.text,
                    "call_id": call_id,
                    "session_id": request.session_id,
                    "provider": session.provider,
                    "message": "WebRTC connection ready. Server can now connect via WebSocket.",
                }
            )

    @app.websocket("/sideband/control/{session_id}")
    async def sideband_control(websocket: WebSocket, session_id: str) -> None:
        """
        WebSocket endpoint for server-side control channel.

        This connects to the same OpenAI session as the user's WebRTC connection,
        allowing the server to:
        - Monitor conversation events
        - Update session instructions
        - Handle tool calls
        - Send server-side messages

        The key insight is that BOTH connections (WebRTC and WebSocket) share
        the same OpenAI session, identified by call_id.

        For Azure OpenAI: wss://{resource}.openai.azure.com/openai/v1/realtime?call_id={call_id}
        For OpenAI: wss://api.openai.com/v1/realtime?call_id={call_id}
        """
        await websocket.accept()

        session = _sessions.get(session_id)
        if not session:
            await websocket.send_json({"type": "error", "message": "Session not found"})
            await websocket.close()
            return

        if not session.call_id:
            await websocket.send_json(
                {"type": "error", "message": "WebRTC not connected yet. No call_id available."}
            )
            await websocket.close()
            return

        _websocket_connections[session_id] = websocket

        is_azure = _is_azure_openai()
        azure_resource = _get_azure_resource()

        # Construct WebSocket URL based on provider
        if is_azure:
            openai_ws_url = f"wss://{azure_resource}.openai.azure.com/openai/v1/realtime?call_id={session.call_id}"
            ws_headers = {"api-key": _get_api_key()}
        else:
            openai_ws_url = f"wss://api.openai.com/v1/realtime?call_id={session.call_id}"
            ws_headers = {
                "Authorization": f"Bearer {_get_api_key()}",
                "OpenAI-Beta": "realtime=v1",
            }

        print(f"\n{'=' * 60}")
        print(f"[SERVER SIDEBAND CONNECTION STARTING]")
        print(f"  Provider: {'Azure OpenAI' if is_azure else 'OpenAI'}")
        print(f"  Session ID: {session_id}")
        print(f"  Call ID: {session.call_id}")
        print(f"  WebSocket URL: {openai_ws_url}")
        print(f"  Connecting server to OpenAI via WebSocket...")
        print(f"  This is the CONTROL CHANNEL - same session as user's WebRTC!")
        print(f"{'=' * 60}\n")

        try:
            import websockets

            async with websockets.connect(
                openai_ws_url, additional_headers=ws_headers
            ) as openai_ws:
                session.websocket_connected = True

                _log_session_info(
                    session,
                    "Server WebSocket Connected",
                    f"Now BOTH user (WebRTC) and server (WebSocket) are connected to the SAME {'Azure ' if is_azure else ''}OpenAI session!",
                )

                await websocket.send_json(
                    {
                        "type": "sideband_connected",
                        "session_id": session_id,
                        "call_id": session.call_id,
                        "provider": "azure" if is_azure else "openai",
                        "message": "Server connected to OpenAI session via sideband WebSocket",
                    }
                )

                async def receive_from_openai():
                    """Receive events from OpenAI and forward to client."""
                    try:
                        async for message in openai_ws:
                            data = json.loads(message)
                            event_type = data.get("type", "unknown")

                            session.events_from_openai += 1
                            session.last_event_type = event_type
                            session.last_activity = datetime.now()

                            # Log interesting events
                            if event_type in [
                                "session.created",
                                "session.updated",
                                "conversation.item.created",
                                "response.created",
                                "response.done",
                                "input_audio_buffer.speech_started",
                                "input_audio_buffer.speech_stopped",
                            ]:
                                _log_session_info(
                                    session,
                                    f"Event from OpenAI: {event_type}",
                                    f"Server sees this via sideband, user's audio flows via WebRTC",
                                )

                            # Forward to client websocket
                            await websocket.send_json({
                                'type': 'openai_event',
                                'event': data,
                                'stats': {
                                    'events_from_openai': session.events_from_openai,
                                    'events_to_openai': session.events_to_openai
                                }
                            })
                    except Exception as e:
                        print(f"[ERROR] Receiving from OpenAI: {e}")
                
                async def receive_from_client():
                    """Receive commands from client and forward to OpenAI."""
                    try:
                        while True:
                            raw = await websocket.receive_text()
                            data = json.loads(raw)
                            
                            # Handle different command types
                            cmd_type = data.get('type', '')
                            
                            if cmd_type == 'session.update':
                                session.events_to_openai += 1
                                _log_session_info(
                                    session,
                                    "Sending session.update via sideband",
                                    f"Instructions: {data.get('session', {}).get('instructions', '')[:50]}..."
                                )
                                await openai_ws.send(json.dumps(data))
                                
                            elif cmd_type == 'conversation.item.create':
                                session.events_to_openai += 1
                                _log_session_info(
                                    session,
                                    "Server adding item to conversation",
                                    "Server can inject messages even while user talks via WebRTC"
                                )
                                await openai_ws.send(json.dumps(data))
                                
                            elif cmd_type == 'response.create':
                                session.events_to_openai += 1
                                _log_session_info(
                                    session,
                                    "Server triggering response",
                                    "Server can trigger AI responses independently of user input"
                                )
                                await openai_ws.send(json.dumps(data))
                                
                            else:
                                # Forward any other events
                                session.events_to_openai += 1
                                await openai_ws.send(json.dumps(data))
                                
                    except WebSocketDisconnect:
                        print(f"[INFO] Client disconnected from sideband: {session_id}")
                    except Exception as e:
                        print(f"[ERROR] Receiving from client: {e}")
                
                # Run both tasks concurrently
                await asyncio.gather(
                    receive_from_openai(),
                    receive_from_client(),
                    return_exceptions=True
                )
                
        except Exception as e:
            print(f"[ERROR] Sideband connection failed: {e}")
            await websocket.send_json({
                'type': 'error',
                'message': f'Failed to connect to OpenAI: {str(e)}'
            })
        finally:
            session.websocket_connected = False
            _websocket_connections.pop(session_id, None)
            _log_session_info(session, "Server WebSocket Disconnected")
            with contextlib.suppress(Exception):
                await websocket.close()

    @app.get('/sideband/sessions')
    async def list_sessions() -> JSONResponse:
        """List all active sideband sessions for monitoring."""
        sessions_data = []
        for session_id, session in _sessions.items():
            sessions_data.append({
                'session_id': session.session_id,
                'call_id': session.call_id,
                'created_at': session.created_at.isoformat(),
                'webrtc_connected': session.webrtc_connected,
                'websocket_connected': session.websocket_connected,
                'events_from_openai': session.events_from_openai,
                'events_to_openai': session.events_to_openai,
                'last_event_type': session.last_event_type,
                'last_activity': session.last_activity.isoformat()
            })
        return JSONResponse({
            'sessions': sessions_data,
            'total': len(sessions_data)
        })

    @app.get('/sideband/session/{session_id}')
    async def get_session(session_id: str) -> JSONResponse:
        """Get details of a specific sideband session."""
        session = _sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        
        return JSONResponse({
            'session_id': session.session_id,
            'call_id': session.call_id,
            'created_at': session.created_at.isoformat(),
            'webrtc_connected': session.webrtc_connected,
            'websocket_connected': session.websocket_connected,
            'events_from_openai': session.events_from_openai,
            'events_to_openai': session.events_to_openai,
            'last_event_type': session.last_event_type,
            'last_activity': session.last_activity.isoformat()
        })

    return app


# HTML page for sideband demo
SIDEBAND_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenAI Realtime Sideband Demo</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
            background: #f5f5f5;
        }
        h1 { color: #333; }
        .provider-badge {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 4px;
            font-size: 0.9rem;
            font-weight: 500;
            margin-left: 1rem;
        }
        .provider-badge.azure { background: #0078d4; color: white; }
        .provider-badge.openai { background: #10a37f; color: white; }
        .description {
            background: #e3f2fd;
            padding: 1rem;
            border-radius: 8px;
            margin-bottom: 2rem;
            border-left: 4px solid #2196f3;
        }
        .architecture {
            background: #fff;
            padding: 1rem;
            border-radius: 8px;
            margin-bottom: 2rem;
            font-family: monospace;
            white-space: pre;
            overflow-x: auto;
        }
        .container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 2rem;
        }
        .panel {
            background: #fff;
            padding: 1.5rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .panel h2 {
            margin-top: 0;
            padding-bottom: 0.5rem;
            border-bottom: 2px solid #eee;
        }
        .panel.webrtc h2 { border-color: #4caf50; }
        .panel.websocket h2 { border-color: #ff9800; }
        .status {
            padding: 0.5rem 1rem;
            border-radius: 4px;
            margin-bottom: 1rem;
            font-weight: 500;
        }
        .status.disconnected { background: #ffebee; color: #c62828; }
        .status.connecting { background: #fff3e0; color: #ef6c00; }
        .status.connected { background: #e8f5e9; color: #2e7d32; }
        button {
            background: #2196f3;
            color: white;
            border: none;
            padding: 0.75rem 1.5rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 1rem;
            margin-right: 0.5rem;
            margin-bottom: 0.5rem;
        }
        button:hover { background: #1976d2; }
        button:disabled { background: #ccc; cursor: not-allowed; }
        button.secondary { background: #ff9800; }
        button.secondary:hover { background: #f57c00; }
        button.danger { background: #f44336; }
        button.danger:hover { background: #d32f2f; }
        .log {
            background: #263238;
            color: #aed581;
            padding: 1rem;
            border-radius: 4px;
            height: 300px;
            overflow-y: auto;
            font-family: 'Consolas', 'Monaco', monospace;
            font-size: 0.85rem;
            line-height: 1.4;
        }
        .log-entry { margin-bottom: 0.25rem; }
        .log-entry.info { color: #81d4fa; }
        .log-entry.success { color: #aed581; }
        .log-entry.error { color: #ef9a9a; }
        .log-entry.event { color: #ce93d8; }
        .session-info {
            background: #f5f5f5;
            padding: 1rem;
            border-radius: 4px;
            margin-bottom: 1rem;
            font-family: monospace;
            font-size: 0.9rem;
        }
        .session-info div { margin-bottom: 0.25rem; }
        .session-info label { color: #666; }
        .session-info span { color: #333; font-weight: 500; }
        .controls { margin-bottom: 1rem; }
        input[type="text"] {
            width: 100%;
            padding: 0.75rem;
            border: 1px solid #ddd;
            border-radius: 4px;
            margin-bottom: 0.5rem;
            font-size: 1rem;
        }
        .full-width { grid-column: 1 / -1; }
    </style>
</head>
<body>
    <h1>🔄 OpenAI Realtime Sideband Demo <span id="provider-badge" class="provider-badge">Loading...</span></h1>
    
    <div class="description">
        <strong>Sideband Architecture:</strong> This demo shows how you can have TWO separate connections to the same OpenAI Realtime session:
        <ul>
            <li><strong>WebRTC (User ↔ OpenAI):</strong> Direct audio/video streaming between user and OpenAI</li>
            <li><strong>WebSocket (Server ↔ OpenAI):</strong> Control channel for session management, tools, and monitoring</li>
        </ul>
        This separation allows audio to flow directly without server bottleneck, while server handles business logic.
        <br><br>
        <strong>Supported Providers:</strong> Azure OpenAI and OpenAI Direct API
    </div>
    
    <div class="architecture">
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
    </div>
    
    <div class="session-info">
        <div><label>Provider: </label><span id="provider-name">Loading...</span></div>
        <div><label>Session ID: </label><span id="session-id">Not created</span></div>
        <div><label>Call ID: </label><span id="call-id">Not available</span></div>
        <div><label>WebRTC Status: </label><span id="webrtc-status">Disconnected</span></div>
        <div><label>WebSocket Status: </label><span id="websocket-status">Disconnected</span></div>
    </div>
    
    <div class="container">
        <div class="panel webrtc">
            <h2>🎤 WebRTC Connection (User ↔ OpenAI)</h2>
            <div id="webrtc-connection-status" class="status disconnected">Disconnected</div>
            <div class="controls">
                <button id="btn-create-session" onclick="createSession()">1. Create Session</button>
                <button id="btn-connect-webrtc" onclick="connectWebRTC()" disabled>2. Connect WebRTC</button>
                <button id="btn-start-audio" onclick="startAudio()" disabled>Start Microphone</button>
                <button id="btn-stop-audio" onclick="stopAudio()" disabled class="danger">Stop Microphone</button>
            </div>
            <div class="log" id="webrtc-log"></div>
        </div>
        
        <div class="panel websocket">
            <h2>🔌 WebSocket Connection (Server ↔ OpenAI)</h2>
            <div id="websocket-connection-status" class="status disconnected">Disconnected</div>
            <div class="controls">
                <button id="btn-connect-websocket" onclick="connectWebSocket()" disabled>3. Connect Server Sideband</button>
                <button id="btn-update-instructions" onclick="updateInstructions()" disabled class="secondary">Update Instructions</button>
                <button id="btn-send-message" onclick="sendServerMessage()" disabled class="secondary">Send Server Message</button>
            </div>
            <input type="text" id="instructions-input" placeholder="Enter new instructions for the AI..." value="You are a helpful assistant. Be concise and friendly.">
            <input type="text" id="message-input" placeholder="Enter a message to inject from server...">
            <div class="log" id="websocket-log"></div>
        </div>
        
        <div class="panel full-width">
            <h2>📊 Session Separation Demo</h2>
            <p>This section shows that both connections share the same OpenAI session but operate independently:</p>
            <div id="separation-log" class="log" style="height: 200px;"></div>
        </div>
    </div>
    
    <script>
        let sessionId = null;
        let callId = null;
        let provider = null;
        let peerConnection = null;
        let localStream = null;
        let dataChannel = null;
        let controlWebSocket = null;
        
        // Load configuration on page load
        async function loadConfig() {
            try {
                const response = await fetch('/sideband/config');
                const config = await response.json();
                provider = config.provider;
                
                const badge = document.getElementById('provider-badge');
                const providerName = document.getElementById('provider-name');
                
                if (provider === 'azure') {
                    badge.textContent = 'Azure OpenAI';
                    badge.className = 'provider-badge azure';
                    providerName.textContent = `Azure OpenAI (${config.azure_resource})`;
                } else {
                    badge.textContent = 'OpenAI';
                    badge.className = 'provider-badge openai';
                    providerName.textContent = 'OpenAI Direct API';
                }
                
                logSeparation(`Provider: ${provider === 'azure' ? 'Azure OpenAI' : 'OpenAI'}`, 'info');
                logSeparation(`Model: ${config.model}`, 'info');
            } catch (error) {
                console.error('Failed to load config:', error);
            }
        }
        
        // Call loadConfig on page load
        loadConfig();
        
        function logWebRTC(message, type = 'info') {
            const log = document.getElementById('webrtc-log');
            const entry = document.createElement('div');
            entry.className = `log-entry ${type}`;
            entry.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
            log.appendChild(entry);
            log.scrollTop = log.scrollHeight;
        }
        
        function logWebSocket(message, type = 'info') {
            const log = document.getElementById('websocket-log');
            const entry = document.createElement('div');
            entry.className = `log-entry ${type}`;
            entry.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
            log.appendChild(entry);
            log.scrollTop = log.scrollHeight;
        }
        
        function logSeparation(message, type = 'info') {
            const log = document.getElementById('separation-log');
            const entry = document.createElement('div');
            entry.className = `log-entry ${type}`;
            entry.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
            log.appendChild(entry);
            log.scrollTop = log.scrollHeight;
        }
        
        function updateSessionDisplay() {
            document.getElementById('session-id').textContent = sessionId || 'Not created';
            document.getElementById('call-id').textContent = callId || 'Not available';
        }
        
        function updateWebRTCStatus(status) {
            const el = document.getElementById('webrtc-connection-status');
            const statusEl = document.getElementById('webrtc-status');
            el.textContent = status;
            statusEl.textContent = status;
            el.className = 'status ' + status.toLowerCase().replace(' ', '-');
        }
        
        function updateWebSocketStatus(status) {
            const el = document.getElementById('websocket-connection-status');
            const statusEl = document.getElementById('websocket-status');
            el.textContent = status;
            statusEl.textContent = status;
            el.className = 'status ' + status.toLowerCase().replace(' ', '-');
        }
        
        async function createSession() {
            try {
                logWebRTC('Creating new sideband session...', 'info');
                const response = await fetch('/sideband/session', { method: 'POST' });
                const data = await response.json();
                
                sessionId = data.session_id;
                provider = data.provider;
                updateSessionDisplay();
                
                logWebRTC(`Session created: ${sessionId}`, 'success');
                logWebRTC(`Provider: ${provider === 'azure' ? 'Azure OpenAI' : 'OpenAI'}`, 'info');
                logSeparation(`New session created: ${sessionId}`, 'success');
                logSeparation(`Provider: ${provider === 'azure' ? 'Azure OpenAI' : 'OpenAI'}`, 'info');
                logSeparation('This session will have TWO separate connections to OpenAI', 'info');
                
                document.getElementById('btn-connect-webrtc').disabled = false;
                document.getElementById('btn-create-session').disabled = true;
            } catch (error) {
                logWebRTC(`Error creating session: ${error.message}`, 'error');
            }
        }
        
        async function connectWebRTC() {
            try {
                logWebRTC('Initializing WebRTC connection...', 'info');
                updateWebRTCStatus('Connecting');
                
                // Create peer connection
                peerConnection = new RTCPeerConnection({
                    iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
                });
                
                // Set up audio playback
                peerConnection.ontrack = (event) => {
                    logWebRTC('Received audio track from OpenAI', 'success');
                    logSeparation('Audio track received via WebRTC (direct from OpenAI)', 'success');
                    const audio = new Audio();
                    audio.srcObject = event.streams[0];
                    audio.play().catch(e => logWebRTC(`Audio play error: ${e.message}`, 'error'));
                };
                
                // Create data channel for events
                dataChannel = peerConnection.createDataChannel('oai-events');
                dataChannel.onopen = () => {
                    logWebRTC('Data channel opened', 'success');
                };
                dataChannel.onmessage = (event) => {
                    try {
                        const data = JSON.parse(event.data);
                        logWebRTC(`Event via WebRTC data channel: ${data.type}`, 'event');
                    } catch (e) {
                        logWebRTC(`Data channel message: ${event.data}`, 'event');
                    }
                };
                
                // *** FIX: Get microphone BEFORE creating SDP offer ***
                logWebRTC('Requesting microphone access...', 'info');
                try {
                    localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    localStream.getTracks().forEach(track => {
                        peerConnection.addTrack(track, localStream);
                        logWebRTC('Microphone track added to peer connection', 'success');
                    });
                } catch (micError) {
                    logWebRTC(`Microphone error: ${micError.message}. Adding receive-only transceiver.`, 'error');
                    // Fallback: receive-only if microphone not available
                    peerConnection.addTransceiver('audio', { direction: 'recvonly' });
                }
                
                // Create and set local offer
                const offer = await peerConnection.createOffer();
                await peerConnection.setLocalDescription(offer);
                
                logWebRTC('Gathering ICE candidates...', 'info');
                
                // Wait for ICE gathering with timeout
                // Some networks never reach 'complete' state, so we use a timeout
                await new Promise((resolve) => {
                    if (peerConnection.iceGatheringState === 'complete') {
                        logWebRTC('ICE gathering already complete', 'info');
                        resolve();
                        return;
                    }
                    
                    // Set a timeout - don't wait forever for ICE gathering
                    const timeout = setTimeout(() => {
                        logWebRTC('ICE gathering timeout - proceeding with available candidates', 'info');
                        resolve();
                    }, 2000);  // 2 second timeout
                    
                    peerConnection.onicegatheringstatechange = () => {
                        logWebRTC(`ICE gathering state: ${peerConnection.iceGatheringState}`, 'info');
                        if (peerConnection.iceGatheringState === 'complete') {
                            clearTimeout(timeout);
                            resolve();
                        }
                    };
                    
                    // Also listen for ICE candidates
                    peerConnection.onicecandidate = (event) => {
                        if (event.candidate) {
                            logWebRTC(`ICE candidate found: ${event.candidate.type || 'unknown'}`, 'info');
                        } else {
                            // null candidate means gathering is done
                            logWebRTC('ICE candidate gathering finished', 'info');
                            clearTimeout(timeout);
                            resolve();
                        }
                    };
                });
                
                logWebRTC('Exchanging SDP offer...', 'info');
                
                // Exchange offer with server
                const response = await fetch('/sideband/offer', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        sdp: peerConnection.localDescription.sdp,
                        session_id: sessionId
                    })
                });
                
                if (!response.ok) {
                    const errorData = await response.json();
                    throw new Error(errorData.detail || 'Failed to exchange offer');
                }
                
                const data = await response.json();
                callId = data.call_id;
                updateSessionDisplay();
                
                // Set remote description
                await peerConnection.setRemoteDescription({
                    type: 'answer',
                    sdp: data.sdp
                });
                
                logWebRTC(`WebRTC connected! Call ID: ${callId}`, 'success');
                logWebRTC(`Provider: ${data.provider === 'azure' ? 'Azure OpenAI' : 'OpenAI'}`, 'info');
                logSeparation(`WebRTC connected with call_id: ${callId}`, 'success');
                logSeparation('User audio now flows DIRECTLY to OpenAI (not through server)', 'info');
                
                // Microphone is already active if we got localStream
                if (localStream) {
                    logWebRTC('Microphone is active - audio going directly to OpenAI via WebRTC', 'success');
                    logSeparation('User microphone active - audio bypasses server completely', 'success');
                    document.getElementById('btn-start-audio').disabled = true;
                    document.getElementById('btn-stop-audio').disabled = false;
                } else {
                    document.getElementById('btn-start-audio').disabled = false;
                }
                
                updateWebRTCStatus('Connected');
                document.getElementById('btn-connect-webrtc').disabled = true;
                document.getElementById('btn-connect-websocket').disabled = false;
                
            } catch (error) {
                logWebRTC(`Error connecting WebRTC: ${error.message}`, 'error');
                updateWebRTCStatus('Disconnected');
            }
        }
        
        async function startAudio() {
            // Microphone is now started during connectWebRTC
            // This function is kept for manual restart if needed
            if (localStream) {
                logWebRTC('Microphone already active', 'info');
                return;
            }
            
            try {
                logWebRTC('Requesting microphone access...', 'info');
                localStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                
                // Note: Adding tracks after negotiation may require renegotiation
                localStream.getTracks().forEach(track => {
                    peerConnection.addTrack(track, localStream);
                });
                
                logWebRTC('Microphone started - audio going directly to OpenAI via WebRTC', 'success');
                logSeparation('User microphone active - audio bypasses server completely', 'success');
                
                document.getElementById('btn-start-audio').disabled = true;
                document.getElementById('btn-stop-audio').disabled = false;
            } catch (error) {
                logWebRTC(`Error starting audio: ${error.message}`, 'error');
            }
        }
        
        function stopAudio() {
            if (localStream) {
                localStream.getTracks().forEach(track => track.stop());
                localStream = null;
                logWebRTC('Microphone stopped', 'info');
                document.getElementById('btn-start-audio').disabled = false;
                document.getElementById('btn-stop-audio').disabled = true;
            }
        }
        
        function connectWebSocket() {
            if (!callId) {
                logWebSocket('No call_id available. Connect WebRTC first.', 'error');
                return;
            }
            
            logWebSocket('Connecting server sideband WebSocket...', 'info');
            logSeparation('Server connecting to SAME OpenAI session via WebSocket', 'info');
            updateWebSocketStatus('Connecting');
            
            const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
            controlWebSocket = new WebSocket(`${protocol}://${location.host}/sideband/control/${sessionId}`);
            
            controlWebSocket.onopen = () => {
                logWebSocket('WebSocket connected to server', 'success');
                updateWebSocketStatus('Connecting to OpenAI');
            };
            
            controlWebSocket.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.type === 'sideband_connected') {
                    logWebSocket(`Server connected to OpenAI session: ${data.call_id}`, 'success');
                    logWebSocket(`Provider: ${data.provider === 'azure' ? 'Azure OpenAI' : 'OpenAI'}`, 'info');
                    logSeparation('SUCCESS: Both WebRTC and WebSocket connected to SAME session!', 'success');
                    logSeparation(`Session has TWO connections sharing call_id: ${data.call_id}`, 'success');
                    updateWebSocketStatus('Connected');
                    
                    document.getElementById('btn-update-instructions').disabled = false;
                    document.getElementById('btn-send-message').disabled = false;
                    document.getElementById('btn-connect-websocket').disabled = true;
                    
                } else if (data.type === 'openai_event') {
                    const eventType = data.event?.type || 'unknown';
                    
                    // Show error details when error event is received
                    if (eventType === 'error') {
                        const errorMsg = data.event?.error?.message || JSON.stringify(data.event);
                        const errorCode = data.event?.error?.code || 'unknown';
                        logWebSocket(`ERROR from OpenAI: [${errorCode}] ${errorMsg}`, 'error');
                        logSeparation(`OpenAI Error: ${errorMsg}`, 'error');
                        console.error('OpenAI Error Details:', data.event);
                    } else {
                        logWebSocket(`OpenAI event: ${eventType}`, 'event');
                        
                        // Show interesting events in separation log
                        if (['response.audio.delta', 'input_audio_buffer.speech_started', 'input_audio_buffer.speech_stopped', 'session.created', 'response.done'].includes(eventType)) {
                            logSeparation(`Server sees: ${eventType} (user audio via WebRTC, events via WebSocket)`, 'event');
                        }
                    }
                    
                } else if (data.type === 'error') {
                    logWebSocket(`Error: ${data.message}`, 'error');
                }
            };
            
            controlWebSocket.onclose = () => {
                logWebSocket('WebSocket disconnected', 'info');
                updateWebSocketStatus('Disconnected');
            };
            
            controlWebSocket.onerror = (error) => {
                logWebSocket(`WebSocket error: ${error}`, 'error');
            };
        }
        
        function updateInstructions() {
            if (!controlWebSocket || controlWebSocket.readyState !== WebSocket.OPEN) {
                logWebSocket('WebSocket not connected', 'error');
                return;
            }
            
            const instructions = document.getElementById('instructions-input').value;
            
            logWebSocket('Sending session.update via sideband...', 'info');
            logSeparation('Server updating session instructions (while user audio flows via WebRTC)', 'info');
            
            controlWebSocket.send(JSON.stringify({
                type: 'session.update',
                session: {
                    type: 'realtime',
                    instructions: instructions
                }
            }));
        }
        
        function sendServerMessage() {
            if (!controlWebSocket || controlWebSocket.readyState !== WebSocket.OPEN) {
                logWebSocket('WebSocket not connected', 'error');
                return;
            }
            
            const message = document.getElementById('message-input').value;
            if (!message) {
                logWebSocket('Enter a message first', 'error');
                return;
            }
            
            logWebSocket('Sending message via sideband...', 'info');
            logSeparation('Server injecting message into conversation (independent of user WebRTC)', 'info');
            
            controlWebSocket.send(JSON.stringify({
                type: 'conversation.item.create',
                item: {
                    type: 'message',
                    role: 'user',
                    content: [{
                        type: 'input_text',
                        text: message
                    }]
                }
            }));
            
            // Trigger response
            controlWebSocket.send(JSON.stringify({
                type: 'response.create'
            }));
            
            document.getElementById('message-input').value = '';
        }
        
        // Clean up on page unload
        window.addEventListener('beforeunload', () => {
            if (localStream) {
                localStream.getTracks().forEach(track => track.stop());
            }
            if (peerConnection) {
                peerConnection.close();
            }
            if (controlWebSocket) {
                controlWebSocket.close();
            }
        });
    </script>
</body>
</html>
"""

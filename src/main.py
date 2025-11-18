import asyncio
import logging
import os
import signal
import uuid
from datetime import datetime, timezone
from typing import Optional

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("ws-proxy")

APP_REVISION = os.getenv("CONTAINER_APP_REVISION", "local")
REALTIME_SUBPROTOCOL = "oai.realtime.v1"
DEFAULT_API_VERSION = "2024-10-01-preview"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing environment variable: {key}")
    return value


def build_realtime_url(endpoint: str, deployment: str, api_version: str) -> str:
    base = endpoint.rstrip("/")
    base = base.replace("https://", "wss://")
    return f"{base}/openai/realtime?api-version={api_version}&deployment={deployment}"


class SignalLogger:
    """Registers handlers that log termination signals for graceful shutdown validation."""

    def __init__(self) -> None:
        self._signals = (signal.SIGTERM, signal.SIGINT)

    def register(self) -> None:
        for sig in self._signals:
            signal.signal(sig, self._handle)

    @staticmethod
    def _handle(signum, frame) -> None:  # type: ignore[override]
        logger.info(
            "signal.received",
            extra={"signal": signum, "revision": APP_REVISION, "ts": _utc_now()},
        )


def create_app() -> FastAPI:
    signal_logger = SignalLogger()
    signal_logger.register()

    app = FastAPI(title="GPT Realtime WebSocket Proxy", version="0.1.0")

    @app.get("/healthz")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "revision": APP_REVISION})

    @app.get("/")
    async def root() -> JSONResponse:
        return JSONResponse(
            {
                "message": "Azure OpenAI Realtime proxy is running.",
                "revision": APP_REVISION,
            }
        )

    @app.websocket("/ws")
    async def websocket_proxy(websocket: WebSocket) -> None:
        connection_id = str(uuid.uuid4())
        await websocket.accept(subprotocol=REALTIME_SUBPROTOCOL)
        logger.info(
            "client.connected",
            extra={"connectionId": connection_id, "revision": APP_REVISION, "ts": _utc_now()},
        )

        azure_ws: Optional[websockets.WebSocketClientProtocol] = None
        try:
            azure_ws = await connect_to_azure()
            await bridge_websockets(connection_id, websocket, azure_ws)
        except WebSocketDisconnect:
            logger.info(
                "client.disconnected",
                extra={"connectionId": connection_id, "reason": "client_disconnect", "ts": _utc_now()},
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(
                "proxy.error",
                extra={"connectionId": connection_id, "error": str(exc), "ts": _utc_now()},
            )
            await websocket.close(code=1011)
        finally:
            if azure_ws:
                await azure_ws.close()
            logger.info(
                "connection.closed",
                extra={"connectionId": connection_id, "ts": _utc_now()},
            )

    return app


async def connect_to_azure() -> websockets.WebSocketClientProtocol:
    endpoint = _require_env("AZURE_OPENAI_ENDPOINT")
    deployment = _require_env("AZURE_OPENAI_DEPLOYMENT")
    api_key = _require_env("AZURE_OPENAI_API_KEY")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION)

    url = build_realtime_url(endpoint, deployment, api_version)
    headers = {
        "api-key": api_key,
        "openai-beta": "realtime=v1",
    }
    logger.info("azure.connect", extra={"url": url, "ts": _utc_now()})
    return await websockets.connect(url, extra_headers=headers, subprotocols=[REALTIME_SUBPROTOCOL])


async def bridge_websockets(
    connection_id: str, client_ws: WebSocket, azure_ws: websockets.WebSocketClientProtocol
) -> None:
    async def client_to_azure() -> None:
        while True:
            message = await client_ws.receive()
            msg_type = message.get("type")
            if msg_type == "websocket.disconnect":
                await azure_ws.close(code=1000)
                break
            data_text = message.get("text")
            data_bytes = message.get("bytes")
            if data_text is not None:
                await azure_ws.send(data_text)
            elif data_bytes is not None:
                await azure_ws.send(data_bytes)

    async def azure_to_client() -> None:
        while True:
            server_message = await azure_ws.recv()
            if isinstance(server_message, str):
                await client_ws.send_text(server_message)
            else:
                await client_ws.send_bytes(server_message)

    await asyncio.gather(client_to_azure(), azure_to_client())
    logger.info(
        "bridge.completed",
        extra={"connectionId": connection_id, "ts": _utc_now()},
    )


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=False,
        log_level="info",
    )

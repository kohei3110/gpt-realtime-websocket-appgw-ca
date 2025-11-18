# pylint: disable=missing-timeout
"""Utility script that keeps WebSocket sessions open to observe ACA revision transitions."""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("ws-client")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hold multiple WebSocket connections for 5 minutes.")
    parser.add_argument("uri", help="WebSocket endpoint exposed by Application Gateway, e.g. ws://host/ws")
    parser.add_argument("--connections", type=int, default=int(os.getenv("WS_CONNECTIONS", "5")), help="Number of concurrent connections")
    parser.add_argument("--duration", type=int, default=int(os.getenv("WS_DURATION_SECONDS", "300")), help="Duration in seconds to keep each connection open")
    parser.add_argument(
        "--ping-interval",
        type=int,
        default=int(os.getenv("WS_PING_INTERVAL", "30")),
        help="Seconds between heartbeat messages",
    )
    return parser.parse_args()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def hold_connection(uri: str, duration: int, ping_interval: int, client_id: str) -> None:
    start = datetime.now(timezone.utc)
    try:
        async with websockets.connect(uri, subprotocols=["oai.realtime.v1"]) as socket:
            LOGGER.info("client.connected", extra={"connectionId": client_id, "ts": now()})
            while (datetime.now(timezone.utc) - start).total_seconds() < duration:
                payload = json.dumps({"type": "heartbeat", "connectionId": client_id, "sentAt": now()})
                await socket.send(payload)
                await asyncio.sleep(ping_interval)
            await socket.close(code=1000)
            LOGGER.info("client.closed", extra={"connectionId": client_id, "ts": now()})
    except Exception as exc:  # pylint: disable=broad-except
        LOGGER.error(
            "client.error",
            extra={"connectionId": client_id, "error": str(exc), "ts": now()},
        )


async def main() -> None:
    args = parse_args()
    tasks = [
        asyncio.create_task(hold_connection(args.uri, args.duration, args.ping_interval, str(uuid.uuid4())))
        for _ in range(args.connections)
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)

# tests/websocket_scaling_test.py
"""WebSocket connection behavior test during Container Apps scaling events."""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
LOGGER = logging.getLogger("ws-scaling-test")


class ConnectionState(Enum):
    """WebSocket connection states."""
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ACTIVE = "active"
    DISCONNECTED = "disconnected"
    ERROR = "error"


@dataclass
class ConnectionMetrics:
    """Metrics for a single WebSocket connection."""
    connection_id: str
    state: ConnectionState = ConnectionState.CONNECTING
    connected_at: Optional[float] = None
    disconnected_at: Optional[float] = None
    messages_sent: int = 0
    messages_received: int = 0
    errors: List[str] = field(default_factory=list)
    last_pong_at: Optional[float] = None
    server_revision: Optional[str] = None
    server_replica_name: Optional[str] = None
    backend_host: Optional[str] = None

    @property
    def duration(self) -> float:
        """Calculate connection duration in seconds."""
        if not self.connected_at:
            return 0.0
        end_time = self.disconnected_at or time.time()
        return end_time - self.connected_at

    @property
    def is_alive(self) -> bool:
        """Check if connection is still alive."""
        return self.state in (ConnectionState.CONNECTED, ConnectionState.ACTIVE)
    
    @property
    def server_info(self) -> str:
        """Get formatted server information."""
        parts = []
        if self.server_revision:
            parts.append(f"revision={self.server_revision}")
        if self.server_replica_name:
            parts.append(f"replica={self.server_replica_name}")
        if self.backend_host:
            parts.append(f"host={self.backend_host}")
        return ", ".join(parts) if parts else "unknown"


class WebSocketScalingTest:
    """Test WebSocket connection behavior during scaling events."""

    def __init__(
        self,
        uri: str,
        num_connections: int,
        duration: int,
        ping_interval: int,
        test_message: str,
        health_endpoint: Optional[str] = None,
    ):
        self.uri = uri
        self.num_connections = num_connections
        self.duration = duration
        self.ping_interval = ping_interval
        self.test_message = test_message
        self.health_endpoint = health_endpoint
        self.metrics: Dict[str, ConnectionMetrics] = {}
        self.start_time = time.time()
        self.stop_event = asyncio.Event()

    def now_iso(self) -> str:
        """Get current time in ISO format."""
        return datetime.now(timezone.utc).isoformat()

    async def fetch_server_info(self, connection_id: str) -> None:
        """Fetch server information from health endpoint."""
        if not self.health_endpoint:
            return
        
        metrics = self.metrics[connection_id]
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(self.health_endpoint, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        data = await response.json()
                        metrics.server_revision = data.get("revision", "unknown")
                        # Extract replica name from environment if available
                        replica = data.get("replica", os.environ.get("HOSTNAME", ""))
                        if replica:
                            metrics.server_replica_name = replica
                        LOGGER.info(f"[{connection_id}] Server info: {metrics.server_info}")
        except Exception as e:
            LOGGER.debug(f"[{connection_id}] Failed to fetch server info: {e}")

    async def send_test_message(self, websocket, connection_id: str) -> None:
        """Send a test message to Azure OpenAI and track responses."""
        metrics = self.metrics[connection_id]
        try:
            payload = json.dumps({"text": self.test_message})
            await websocket.send(payload)
            metrics.messages_sent += 1
            LOGGER.debug(f"[{connection_id}] Sent test message")

            # Wait for responses (with timeout)
            response_timeout = 30
            start = time.time()
            while time.time() - start < response_timeout:
                try:
                    response = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    data = json.loads(response)
                    metrics.messages_received += 1
                    
                    # Check for revision info in health response
                    if data.get("type") == "status" and "revision" in data:
                        metrics.server_revision = data["revision"]
                    
                    # Break on completion or error
                    if data.get("type") in ("status", "error"):
                        if "complete" in data.get("message", "").lower():
                            break
                        if data.get("type") == "error":
                            LOGGER.warning(f"[{connection_id}] Response error: {data.get('message')}")
                            break
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    LOGGER.warning(f"[{connection_id}] Error receiving response: {e}")
                    break

        except Exception as e:
            error_msg = f"Failed to send test message: {e}"
            metrics.errors.append(error_msg)
            LOGGER.error(f"[{connection_id}] {error_msg}")

    async def maintain_connection(self, connection_id: str) -> None:
        """Maintain a single WebSocket connection and monitor its health."""
        metrics = ConnectionMetrics(connection_id=connection_id)
        self.metrics[connection_id] = metrics

        try:
            LOGGER.info(f"[{connection_id}] Connecting to {self.uri}")
            async with websockets.connect(
                self.uri,
                ping_interval=None,  # We'll handle pings manually
                close_timeout=10,
            ) as websocket:
                metrics.state = ConnectionState.CONNECTED
                metrics.connected_at = time.time()
                
                # Extract backend host from websocket if available
                if hasattr(websocket, 'remote_address'):
                    try:
                        metrics.backend_host = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
                    except Exception:
                        pass
                
                # Fetch server info from health endpoint
                await self.fetch_server_info(connection_id)
                
                LOGGER.info(f"[{connection_id}] Connected at {self.now_iso()} -> {metrics.server_info}")

                # Initial test message to establish session
                await self.send_test_message(websocket, connection_id)
                metrics.state = ConnectionState.ACTIVE

                # Maintain connection with periodic health checks
                last_ping = time.time()
                ping_count = 0
                while not self.stop_event.is_set():
                    elapsed = time.time() - self.start_time
                    if elapsed >= self.duration:
                        LOGGER.info(f"[{connection_id}] Test duration reached, closing")
                        break

                    # Send periodic ping/heartbeat
                    if time.time() - last_ping >= self.ping_interval:
                        try:
                            # Try to get health status
                            await websocket.ping()
                            metrics.last_pong_at = time.time()
                            last_ping = time.time()
                            ping_count += 1
                            
                            # Periodically log connection status
                            if ping_count % 5 == 0:
                                LOGGER.info(f"[{connection_id}] Still connected ({metrics.server_info}) - {ping_count} pings")
                            else:
                                LOGGER.debug(f"[{connection_id}] Ping #{ping_count} successful")
                        except Exception as e:
                            error_msg = f"Ping failed: {e}"
                            metrics.errors.append(error_msg)
                            LOGGER.warning(f"[{connection_id}] {error_msg}")
                            break

                    await asyncio.sleep(1)

                # Graceful close
                await websocket.close(code=1000, reason="Test completed")
                metrics.state = ConnectionState.DISCONNECTED
                metrics.disconnected_at = time.time()
                LOGGER.info(
                    f"[{connection_id}] Closed gracefully after {metrics.duration:.1f}s (was on {metrics.server_info})"
                )

        except ConnectionClosed as e:
            metrics.state = ConnectionState.DISCONNECTED
            metrics.disconnected_at = time.time()
            error_msg = f"Connection closed: code={e.code}, reason={e.reason}"
            metrics.errors.append(error_msg)
            LOGGER.warning(f"[{connection_id}] {error_msg} (duration: {metrics.duration:.1f}s, was on {metrics.server_info})")

        except Exception as e:
            metrics.state = ConnectionState.ERROR
            metrics.disconnected_at = time.time()
            error_msg = f"Unexpected error: {type(e).__name__}: {e}"
            metrics.errors.append(error_msg)
            LOGGER.error(f"[{connection_id}] {error_msg} (was on {metrics.server_info})")

    async def run(self) -> None:
        """Run the scaling test with multiple connections."""
        LOGGER.info(f"Starting WebSocket scaling test")
        LOGGER.info(f"  URI: {self.uri}")
        LOGGER.info(f"  Connections: {self.num_connections}")
        LOGGER.info(f"  Duration: {self.duration}s")
        LOGGER.info(f"  Ping interval: {self.ping_interval}s")
        if self.health_endpoint:
            LOGGER.info(f"  Health endpoint: {self.health_endpoint}")
        LOGGER.info("=" * 80)

        # Create connection tasks
        tasks = []
        for i in range(self.num_connections):
            connection_id = f"conn-{i+1:03d}-{uuid.uuid4().hex[:8]}"
            task = asyncio.create_task(self.maintain_connection(connection_id))
            tasks.append(task)
            # Stagger connection creation slightly to avoid overwhelming the server
            await asyncio.sleep(0.1)

        # Wait for all tasks to complete or stop event
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            LOGGER.error(f"Error during test execution: {e}")
        finally:
            self.stop_event.set()

    def print_summary(self) -> None:
        """Print test summary and statistics."""
        LOGGER.info("=" * 80)
        LOGGER.info("TEST SUMMARY")
        LOGGER.info("=" * 80)

        total_connections = len(self.metrics)
        successful = sum(1 for m in self.metrics.values() if m.state == ConnectionState.DISCONNECTED and not m.errors)
        failed = sum(1 for m in self.metrics.values() if m.state == ConnectionState.ERROR or m.errors)
        still_connected = sum(1 for m in self.metrics.values() if m.is_alive)

        LOGGER.info(f"Total connections: {total_connections}")
        LOGGER.info(f"  Successful: {successful}")
        LOGGER.info(f"  Failed: {failed}")
        LOGGER.info(f"  Still connected: {still_connected}")

        if self.metrics:
            durations = [m.duration for m in self.metrics.values() if m.duration > 0]
            if durations:
                avg_duration = sum(durations) / len(durations)
                max_duration = max(durations)
                min_duration = min(durations)
                LOGGER.info(f"\nConnection duration:")
                LOGGER.info(f"  Average: {avg_duration:.1f}s")
                LOGGER.info(f"  Max: {max_duration:.1f}s")
                LOGGER.info(f"  Min: {min_duration:.1f}s")

            total_messages = sum(m.messages_sent for m in self.metrics.values())
            total_received = sum(m.messages_received for m in self.metrics.values())
            LOGGER.info(f"\nMessages:")
            LOGGER.info(f"  Sent: {total_messages}")
            LOGGER.info(f"  Received: {total_received}")

            # Group by server revision
            revisions = {}
            for m in self.metrics.values():
                if m.server_revision:
                    revisions[m.server_revision] = revisions.get(m.server_revision, 0) + 1
            if revisions:
                LOGGER.info(f"\nServer revisions:")
                for rev, count in sorted(revisions.items()):
                    LOGGER.info(f"  {rev}: {count} connections")
            
            # Group by replica
            replicas = {}
            for m in self.metrics.values():
                if m.server_replica_name:
                    replicas[m.server_replica_name] = replicas.get(m.server_replica_name, 0) + 1
            if replicas:
                LOGGER.info(f"\nServer replicas:")
                for replica, count in sorted(replicas.items()):
                    LOGGER.info(f"  {replica}: {count} connections")
            
            # Group by backend host
            backends = {}
            for m in self.metrics.values():
                if m.backend_host:
                    backends[m.backend_host] = backends.get(m.backend_host, 0) + 1
            if backends:
                LOGGER.info(f"\nBackend hosts:")
                for host, count in sorted(backends.items()):
                    LOGGER.info(f"  {host}: {count} connections")

        # List connections with errors
        errors = [(conn_id, m) for conn_id, m in self.metrics.items() if m.errors]
        if errors:
            LOGGER.info(f"\nConnections with errors ({len(errors)}):")
            for conn_id, m in errors[:10]:  # Show first 10
                error_summary = ', '.join(m.errors[:3])
                LOGGER.info(f"  [{conn_id}] ({m.server_info}) {error_summary}")
            if len(errors) > 10:
                LOGGER.info(f"  ... and {len(errors) - 10} more")

        LOGGER.info("=" * 80)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Test WebSocket connection behavior during Container Apps scaling",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  # Basic test with 10 connections for 5 minutes
  python tests/websocket_scaling_test.py wss://your-host.example.com/chat

  # With health endpoint to fetch server info
  python tests/websocket_scaling_test.py wss://your-host.example.com/chat \\
    --health-endpoint https://your-host.example.com/healthz

  # Stress test with 50 connections for 10 minutes
  python tests/websocket_scaling_test.py wss://your-host.example.com/chat --connections 50 --duration 600

  # Quick test with frequent pings
  python tests/websocket_scaling_test.py wss://your-host.example.com/chat --connections 5 --duration 120 --ping-interval 5

During the test, you can:
  1. Scale out: az containerapp update --name <app> --resource-group <rg> --min-replicas 2 --max-replicas 3
  2. Scale in: az containerapp update --name <app> --resource-group <rg> --min-replicas 1 --max-replicas 2
  3. Deploy new revision: azd deploy
        """,
    )
    parser.add_argument(
        "uri",
        help="WebSocket endpoint (e.g., wss://your-host.example.com/chat)",
    )
    parser.add_argument(
        "--health-endpoint",
        type=str,
        default=os.getenv("WS_HEALTH_ENDPOINT", ""),
        help="Health endpoint URL to fetch server revision info (e.g., https://your-host.example.com/healthz)",
    )
    parser.add_argument(
        "--connections",
        type=int,
        default=int(os.getenv("WS_CONNECTIONS", "10")),
        help="Number of concurrent connections (default: 10)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=int(os.getenv("WS_DURATION", "300")),
        help="Test duration in seconds (default: 300 = 5 minutes)",
    )
    parser.add_argument(
        "--ping-interval",
        type=int,
        default=int(os.getenv("WS_PING_INTERVAL", "30")),
        help="Seconds between ping messages (default: 30)",
    )
    parser.add_argument(
        "--test-message",
        type=str,
        default=os.getenv("WS_TEST_MESSAGE", "Hello, what is 2+2?"),
        help="Test message to send through WebSocket (default: 'Hello, what is 2+2?')",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging (DEBUG level)",
    )
    return parser.parse_args()


async def main() -> None:
    """Main entry point."""
    args = parse_args()

    if args.verbose:
        LOGGER.setLevel(logging.DEBUG)

    test = WebSocketScalingTest(
        uri=args.uri,
        num_connections=args.connections,
        duration=args.duration,
        ping_interval=args.ping_interval,
        test_message=args.test_message,
        health_endpoint=args.health_endpoint if args.health_endpoint else None,
    )

    # Handle graceful shutdown
    def signal_handler(sig, frame):
        LOGGER.info("\nReceived interrupt signal, stopping test...")
        test.stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await test.run()
    finally:
        test.print_summary()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
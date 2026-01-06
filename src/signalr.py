"""Azure SignalR Service (serverless) helpers.

This module provides:
- Connection string parsing
- HS256 JWT generation for Azure SignalR client and REST API
- REST helpers for sending to users using the SignalR REST API

Official constraints we follow (serverless):
- Clients are LISTEN mode (do not send messages to SignalR from browser)
- negotiate returns {url, accessToken}
- REST auth JWT: aud must match the HTTP request URL base (query removed, no trailing slash)
- User targeting requires nameid claim on client token (userId)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import jwt


@dataclass(frozen=True)
class SignalRConnectionInfo:
    endpoint: str
    access_key_bytes: bytes


def normalize_audience(url: str) -> str:
    """Normalize audience URL for SignalR REST auth.

    - removes query
    - removes trailing slash

    Examples:
      https://example/.../hubs/myhub/ -> https://example/.../hubs/myhub
      https://example/.../hubs/myhub?x=1 -> https://example/.../hubs/myhub
    """

    if not url:
        return ""

    # urlsplit keeps scheme/netloc/path separate; query is ignored for aud.
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else url.split("?", 1)[0]
    return base.rstrip("/")


def parse_signalr_connection_string(connection_string: str) -> SignalRConnectionInfo:
    """Parse Azure SignalR connection string.

    Expected format (order may vary):
      Endpoint=https://<name>.service.signalr.net;AccessKey=...;Version=1.0;

    IMPORTANT: For HS256 signing, use the AccessKey *string* as-is (UTF-8 bytes).
    Do NOT base64-decode it.
    """

    if not connection_string or not connection_string.strip():
        raise ValueError("AZURE_SIGNALR_CONNECTION_STRING is empty")

    items: dict[str, str] = {}
    for segment in connection_string.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if "=" not in segment:
            continue
        k, v = segment.split("=", 1)
        items[k.strip().lower()] = v.strip()

    endpoint = items.get("endpoint", "").rstrip("/")
    if not endpoint:
        raise ValueError("SignalR connection string missing Endpoint")

    access_key = items.get("accesskey", "")
    if not access_key:
        raise ValueError("SignalR connection string missing AccessKey")

    access_key_bytes = access_key.encode("utf-8")
    if not access_key_bytes:
        raise ValueError("SignalR connection string AccessKey encoded to empty bytes")

    return SignalRConnectionInfo(endpoint=endpoint, access_key_bytes=access_key_bytes)


def create_hs256_jwt(
    *,
    secret: bytes,
    audience: str,
    expires_in_seconds: int,
    claims: dict[str, Any] | None = None,
) -> str:
    """Create an HS256 JWT for Azure SignalR.

    SignalR requires:
    - aud
    - exp

    We also set iat.
    """

    if not audience:
        raise ValueError("audience must not be empty")
    if expires_in_seconds <= 0:
        raise ValueError("expires_in_seconds must be positive")

    now = int(time.time())
    payload: dict[str, Any] = {
        "aud": audience,
        "iat": now,
        "exp": now + int(expires_in_seconds),
    }
    if claims:
        payload.update(claims)

    token = jwt.encode(payload, secret, algorithm="HS256")
    # PyJWT may return bytes in older versions; normalize.
    if isinstance(token, bytes):
        return token.decode("utf-8")
    return token


class SignalRService:
    """Minimal client for Azure SignalR serverless negotiate + REST send."""

    def __init__(self, *, connection_info: SignalRConnectionInfo, hub_name: str) -> None:
        if not hub_name:
            raise ValueError("hub_name must not be empty")
        self._info = connection_info
        self._hub = hub_name

    @property
    def hub_name(self) -> str:
        return self._hub

    def build_client_url(self) -> str:
        # This URL becomes the JWT audience for *client* token.
        return f"{self._info.endpoint}/client/?hub={self._hub}"

    def build_rest_hub_url(self) -> str:
        # This URL becomes the JWT audience for *REST* tokens (normalized).
        return f"{self._info.endpoint}/api/v1/hubs/{self._hub}"

    def negotiate(self, *, user_id: str, expires_in_seconds: int = 60 * 60) -> dict[str, str]:
        if not user_id:
            raise ValueError("user_id must not be empty")

        url = self.build_client_url()
        token = create_hs256_jwt(
            secret=self._info.access_key_bytes,
            audience=url,
            expires_in_seconds=expires_in_seconds,
            claims={"nameid": user_id},
        )
        return {"url": url, "accessToken": token}

    def _build_rest_send_to_user_request(
        self,
        *,
        user_id: str,
        invocation: dict[str, Any],
        token_expires_in_seconds: int = 60 * 5,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Build request URL/headers/body for REST send-to-user.

        Important: per official spec, JWT `aud` must match the *HTTP request URL*
        (query removed, no trailing slash).
        """

        if not user_id:
            raise ValueError("user_id must not be empty")

        rest_hub_url = self.build_rest_hub_url()
        safe_user_id = quote(user_id, safe="")
        request_url = f"{rest_hub_url}/users/{safe_user_id}"
        aud = normalize_audience(request_url)

        token = create_hs256_jwt(
            secret=self._info.access_key_bytes,
            audience=aud,
            expires_in_seconds=token_expires_in_seconds,
            claims=None,
        )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        return request_url, headers, invocation

    async def send_to_user(
        self,
        *,
        user_id: str,
        invocation: dict[str, Any],
        timeout_seconds: float = 10.0,
    ) -> httpx.Response:
        """Send a JSON protocol invocation to a specific user via REST API."""

        request_url, headers, body = self._build_rest_send_to_user_request(
            user_id=user_id,
            invocation=invocation,
        )

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            return await client.post(request_url, headers=headers, json=body)

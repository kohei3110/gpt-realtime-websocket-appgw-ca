import time

import jwt
import pytest

from src.signalr import (
    SignalRConnectionInfo,
    SignalRService,
    create_hs256_jwt,
    normalize_audience,
    parse_signalr_connection_string,
)


def test_normalize_audience_strips_query_and_trailing_slash() -> None:
    assert (
        normalize_audience(
            "https://example.service.signalr.net/api/v1/hubs/myhub/?api-version=1"
        )
        == "https://example.service.signalr.net/api/v1/hubs/myhub"
    )
    assert (
        normalize_audience("https://example.service.signalr.net/api/v1/hubs/myhub/")
        == "https://example.service.signalr.net/api/v1/hubs/myhub"
    )
    assert (
        normalize_audience("https://example.service.signalr.net/api/v1/hubs/myhub")
        == "https://example.service.signalr.net/api/v1/hubs/myhub"
    )


def test_parse_signalr_connection_string_uses_access_key_string_bytes_as_is() -> None:
    access_key = "super-secret-bytes"
    cs = (
        "Endpoint=https://unit.service.signalr.net;"
        f"AccessKey={access_key};"
        "Version=1.0;"
    )

    info = parse_signalr_connection_string(cs)
    assert isinstance(info, SignalRConnectionInfo)
    assert info.endpoint == "https://unit.service.signalr.net"
    assert info.access_key_bytes == access_key.encode("utf-8")


def test_parse_signalr_connection_string_accepts_non_base64ish_access_key() -> None:
    access_key = "not-base64!!"
    cs = (
        "Endpoint=https://unit.service.signalr.net;"
        f"AccessKey={access_key};"
        "Version=1.0;"
    )

    info = parse_signalr_connection_string(cs)
    assert info.endpoint == "https://unit.service.signalr.net"
    assert info.access_key_bytes == access_key.encode("utf-8")


def test_create_hs256_jwt_contains_exp_and_aud_and_optional_nameid() -> None:
    secret = b"jwt-secret"
    aud = "https://example.service.signalr.net/api/v1/hubs/myhub"

    before = int(time.time())
    token = create_hs256_jwt(secret=secret, audience=aud, expires_in_seconds=60, claims={"nameid": "abc"})
    # Inspect claims without verifying signature/audience (PoC requirement).
    decoded = jwt.decode(
        token,
        options={"verify_signature": False, "verify_aud": False},
    )

    assert decoded["aud"] == aud
    assert decoded["nameid"] == "abc"
    assert "exp" in decoded
    assert decoded["exp"] >= before


def test_create_hs256_jwt_rejects_empty_audience() -> None:
    with pytest.raises(ValueError):
        create_hs256_jwt(secret=b"s", audience="", expires_in_seconds=60)


def test_signalr_rest_jwt_audience_matches_request_url() -> None:
    # REST auth spec: `aud` must be the HTTP request URL (query removed, no trailing slash)
    raw_secret = b"super-secret-bytes"
    info = SignalRConnectionInfo(endpoint="https://unit.service.signalr.net", access_key_bytes=raw_secret)
    svc = SignalRService(connection_info=info, hub_name="myhub")

    request_url, headers, _ = svc._build_rest_send_to_user_request(
        user_id="abc",
        invocation={"target": "x", "arguments": [1]},
        token_expires_in_seconds=60,
    )

    assert request_url == "https://unit.service.signalr.net/api/v1/hubs/myhub/users/abc"
    auth = headers.get("Authorization", "")
    assert auth.startswith("Bearer ")
    token = auth.split(" ", 1)[1]

    decoded = jwt.decode(
        token,
        options={"verify_signature": False, "verify_aud": False},
    )
    assert decoded["aud"] == normalize_audience(request_url)

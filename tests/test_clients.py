"""The human approval gate, HTTP authentication and the web client's origin check."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from docker2k8s_mcp import main as server_main
from docker2k8s_mcp.client import common
from docker2k8s_mcp.http_auth import BearerTokenMiddleware, is_loopback


# --------------------------------------------------------------------------
# Approval gate
# --------------------------------------------------------------------------
@pytest.mark.parametrize("dry_run", [None, False, "false", "no", "0", 0])
def test_anything_but_a_literal_true_dry_run_needs_a_human(dry_run: Any) -> None:
    # The server coerces all of these to False and would deploy for real.
    assert common.needs_approval("apply_manifests", {"approved": True, "dry_run": dry_run})


def test_real_dry_runs_and_unapproved_calls_skip_the_prompt() -> None:
    assert not common.needs_approval("apply_manifests", {"approved": True, "dry_run": True})
    assert not common.needs_approval("apply_manifests", {"approved": False})  # server refuses it
    assert not common.needs_approval("get_pods", {"approved": True})


def test_what_the_human_approved_is_what_runs() -> None:
    args = common.approved_arguments({"path": "x", "approved": "yes", "dry_run": "false"})

    assert args == {"path": "x", "approved": True, "dry_run": False}


def test_tool_results_are_truncated_for_the_model() -> None:
    result = NS(structured_content={"blob": "x" * (common.MAX_TOOL_RESULT_CHARS * 2)}, content=[])

    text = common.result_text(result)

    assert len(text) < common.MAX_TOOL_RESULT_CHARS + 100
    assert text.endswith("characters]")


# --------------------------------------------------------------------------
# HTTP authentication
# --------------------------------------------------------------------------
async def _request(app: Any, headers: list[tuple[bytes, bytes]]) -> int:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app({"type": "http", "headers": headers}, receive, send)
    return sent[0]["status"]


async def test_bearer_token_is_required() -> None:
    async def ok_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})

    app = BearerTokenMiddleware(ok_app, "s3cret")

    assert await _request(app, []) == 401
    assert await _request(app, [(b"authorization", b"Bearer wrong")]) == 401
    assert await _request(app, [(b"authorization", b"Bearer s3cret")]) == 200


def test_loopback_detection() -> None:
    assert all(is_loopback(h) for h in ("127.0.0.1", "localhost", "::1", "[::1]"))
    assert not any(is_loopback(h) for h in ("0.0.0.0", "192.168.1.5", "example.com"))


def test_server_refuses_public_bind_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    server_main.get_settings.cache_clear()

    assert server_main.main(["--transport", "http", "--host", "0.0.0.0"]) == 2


# --------------------------------------------------------------------------
# Web client
# --------------------------------------------------------------------------
async def test_web_client_rejects_cross_origin_websockets() -> None:
    pytest.importorskip("aiohttp")
    pytest.importorskip("openai")
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import web_client

    app = web.Application()
    app.router.add_get("/ws", web_client.handle_websocket)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/ws", headers={"Origin": "https://evil.example"})
        assert response.status == 403

        ws = await client.ws_connect("/ws", headers={"Origin": f"http://localhost:{web_client.WEB_PORT}"})
        message = await ws.receive_json()  # no MCP session in tests: an explanatory error
        assert message["type"] == "error"
        await ws.close()

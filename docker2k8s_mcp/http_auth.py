"""Shared-token authentication for the HTTP transports.

The MCP SDK's built-in auth is OAuth-shaped (issuer, resource metadata). A
single deployment tool needs far less: one secret the operator hands to their
client. This middleware checks it on every HTTP request before the MCP app sees
anything.
"""

from __future__ import annotations

import hmac
import ipaddress
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class BearerTokenMiddleware:
    """Reject any HTTP request that does not carry ``Authorization: Bearer <token>``."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan events carry no headers and must reach the app untouched.
        if scope["type"] == "http":
            supplied = next(
                (value for name, value in scope.get("headers", ()) if name == b"authorization"),
                b"",
            )
            if not hmac.compare_digest(supplied, self._expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"text/plain; charset=utf-8"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def is_loopback(host: str) -> bool:
    """True when ``host`` only accepts connections from this machine."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False

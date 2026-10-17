"""Entry point for the docker2k8s MCP server.

Run it over whichever transport your MCP client speaks:

    docker2k8s-mcp                          # stdio (default; Claude Desktop, Cursor, ...)
    docker2k8s-mcp --transport http         # streamable HTTP on 127.0.0.1:8000/mcp
    docker2k8s-mcp --transport sse          # legacy SSE, for older clients

The HTTP transports require MCP_AUTH_TOKEN whenever they listen on anything
other than loopback, because every tool -- including apply_manifests -- is
reachable by whoever can connect.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import LOG_LEVELS, configure_logging, get_settings
from .http_auth import BearerTokenMiddleware, is_loopback
from .server import SERVER_NAME, build_server

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="docker2k8s-mcp",
        description="MCP server for AI-assisted Docker to Kubernetes migration.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help=(
            "MCP transport. 'stdio' for clients that launch the server as a subprocess "
            "(the default), 'http' for streamable HTTP, 'sse' for older HTTP clients."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for http/sse.")
    parser.add_argument("--port", type=int, default=8000, help="Port for http/sse.")
    parser.add_argument(
        "--path",
        default=None,
        help="HTTP path to serve on. Defaults to /mcp for http and /sse for sse.",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="Streamable HTTP without session state, for load-balanced deployments.",
    )
    parser.add_argument(
        "--log-level", type=str.upper, choices=LOG_LEVELS, default=None, help="Override LOG_LEVEL."
    )
    parser.add_argument(
        "--insecure-no-auth",
        action="store_true",
        help=(
            "Allow http/sse on a non-loopback address without MCP_AUTH_TOKEN. Only for "
            "servers behind an authenticating proxy."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    settings = get_settings()
    if args.log_level:
        settings.log_level = args.log_level
    configure_logging(settings)

    mcp = build_server()

    if args.transport == "stdio":
        # stdout carries the protocol here, so nothing else may be printed to it.
        logger.info("Starting %s on stdio", SERVER_NAME)
        mcp.run("stdio")
        return 0

    token = settings.mcp_auth_token
    if not token and not is_loopback(args.host) and not args.insecure_no_auth:
        logger.error(
            "Refusing to serve on %s without authentication: anyone who can reach it could "
            "deploy to your cluster. Set MCP_AUTH_TOKEN, bind to 127.0.0.1, or pass "
            "--insecure-no-auth if an authenticating proxy sits in front.",
            args.host,
        )
        return 2

    if args.transport == "http":
        path = args.path or "/mcp"
        app = mcp.streamable_http_app(
            streamable_http_path=path, stateless_http=args.stateless, host=args.host
        )
    else:
        path = args.path or "/sse"
        app = mcp.sse_app(sse_path=path, host=args.host)

    logger.info(
        "Starting %s on http://%s:%s%s (%s, %s)",
        SERVER_NAME,
        args.host,
        args.port,
        path,
        args.transport,
        "token required" if token else "no auth",
    )
    import uvicorn

    uvicorn.run(
        BearerTokenMiddleware(app, token) if token else app,
        host=args.host,
        port=args.port,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

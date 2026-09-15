"""Entry point for the docker2k8s MCP server.

Run it over whichever transport your MCP client speaks:

    docker2k8s-mcp                          # stdio (default; Claude Desktop, Cursor, ...)
    docker2k8s-mcp --transport http         # streamable HTTP on 127.0.0.1:8000/mcp
    docker2k8s-mcp --transport sse          # legacy SSE, for older clients
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import configure_logging, get_settings
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
    parser.add_argument("--log-level", default=None, help="Override LOG_LEVEL.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    settings = get_settings()
    if args.log_level:
        settings.log_level = args.log_level.upper()
    configure_logging(settings)

    mcp = build_server()

    if args.transport == "stdio":
        # stdout carries the protocol here, so nothing else may be printed to it.
        logger.info("Starting %s on stdio", SERVER_NAME)
        mcp.run("stdio")
        return 0

    if args.transport == "http":
        path = args.path or "/mcp"
        logger.info("Starting %s on http://%s:%s%s", SERVER_NAME, args.host, args.port, path)
        mcp.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=path,
            stateless_http=args.stateless,
        )
        return 0

    path = args.path or "/sse"
    logger.info("Starting %s on http://%s:%s%s (SSE)", SERVER_NAME, args.host, args.port, path)
    mcp.run("sse", host=args.host, port=args.port, sse_path=path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

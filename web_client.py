"""Web-based chat client for the docker2k8s MCP server.

Serves a chat UI at http://localhost:3000 and bridges the browser to both
OpenAI (for LLM reasoning) and the MCP server (for tool execution).

Prerequisites:
    1. The MCP server must be running:  run.bat   (or docker2k8s-mcp --transport http)
    2. OPENAI_API_KEY must be set in .env

Usage:
    python web_client.py
    # Then open http://localhost:3000
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

# Make the package importable when run from a checkout without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp import ClientSession  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from docker2k8s_mcp.client.common import (  # noqa: E402
    DECLINED_MESSAGE,
    SYSTEM_PROMPT,
    approved_arguments,
    connect_http,
    needs_approval,
    openai_tools,
    result_text,
)
from docker2k8s_mcp.config import configure_logging, get_settings  # noqa: E402

logger = logging.getLogger("web_client")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MCP_URL = "http://127.0.0.1:8000/mcp"
WEB_HOST = "127.0.0.1"
WEB_PORT = 3000
MAX_STEPS = 40
#: Browser messages are short chat prompts and approval clicks.
MAX_WS_MESSAGE_BYTES = 256 * 1024
MAX_PROMPT_CHARS = 20_000

#: Browsers do not apply the same-origin policy to WebSockets, so without this
#: check any page open in the browser could drive the agent and approve deploys.
ALLOWED_ORIGINS = frozenset({f"http://localhost:{WEB_PORT}", f"http://127.0.0.1:{WEB_PORT}"})


# ═══════════════════════════════════════════════════════════════════════════
# MCP Bridge — persistent connection to the MCP server
# ═══════════════════════════════════════════════════════════════════════════


class MCPBridge:
    """Holds the MCP session and tool definitions."""

    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self.tools_for_openai: list[dict[str, Any]] = []
        self.tools_info: list[dict[str, str]] = []
        self.server_name: str = ""
        self.instructions: str = ""
        self.startup_error: str = ""

    async def connect(self, stack: AsyncExitStack) -> None:
        self.session = await connect_http(stack, MCP_URL, get_settings().mcp_auth_token)
        init = await self.session.initialize()

        self.server_name = init.server_info.name
        self.instructions = init.instructions or ""

        tools = (await self.session.list_tools()).tools
        self.tools_for_openai = openai_tools(tools)
        self.tools_info = [
            {"name": t.name, "description": t.description or t.title or t.name}
            for t in tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, str]:
        """Call an MCP tool.  Returns ``(status, text)``."""
        assert self.session is not None
        try:
            result = await self.session.call_tool(name, arguments)
            return ("error" if result.is_error else "ok"), result_text(result)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            return "error", f"Tool call failed: {exc}"


# Global bridge instance (single-user, local-only server).
bridge = MCPBridge()


# ═══════════════════════════════════════════════════════════════════════════
# WebSocket handler — one per browser tab
# ═══════════════════════════════════════════════════════════════════════════


async def handle_websocket(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Origin") not in ALLOWED_ORIGINS:
        logger.warning("Rejected WebSocket from origin %r", request.headers.get("Origin"))
        raise web.HTTPForbidden(text="Cross-origin WebSocket connections are not allowed.")

    ws = web.WebSocketResponse(max_msg_size=MAX_WS_MESSAGE_BYTES)
    await ws.prepare(request)

    if not bridge.session:
        await ws.send_json(
            {
                "type": "error",
                "message": bridge.startup_error
                or "MCP server not connected. Make sure it is running on port 8000 (run.bat).",
            }
        )
        await ws.close()
        return ws

    settings = get_settings()
    openai_client = AsyncOpenAI(
        api_key=settings.openai_api_key, base_url=settings.openai_base_url
    )
    model = settings.openai_model

    # Fresh conversation per WebSocket connection.
    system = SYSTEM_PROMPT
    if bridge.instructions:
        system += f"\n\n--- Server instructions ---\n{bridge.instructions}"

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    # Approval-gate synchronisation: one pending approval at a time, bound to
    # the id of the tool call it was requested for.
    pending_approval: dict[str, Any] = {}
    tool_counter = 0
    chat_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Chat loop — runs as a background task so the WS reader stays free
    # to receive approval responses.
    # ------------------------------------------------------------------

    async def run_chat(prompt: str) -> None:
        nonlocal tool_counter

        messages.append({"role": "user", "content": prompt})

        for _step in range(MAX_STEPS):
            # ---- Call OpenAI (streaming) ---------------------------------
            stream = await openai_client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                tools=bridge.tools_for_openai,  # type: ignore[arg-type]
                tool_choice="auto",
                stream=True,
            )

            content_parts: list[str] = []
            tc_map: dict[int, dict[str, str]] = {}

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta.content:
                    content_parts.append(delta.content)
                    await ws.send_json(
                        {"type": "assistant_chunk", "content": delta.content}
                    )

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        entry = tc_map.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            entry["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                entry["name"] = tc.function.name
                            if tc.function.arguments:
                                entry["arguments"] += tc.function.arguments

            full_content = "".join(content_parts)
            tool_calls = [tc_map[k] for k in sorted(tc_map)]

            # Build assistant message for OpenAI history.
            msg: dict[str, Any] = {"role": "assistant"}
            if full_content:
                msg["content"] = full_content
            if tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(msg)

            await ws.send_json({"type": "assistant_done"})

            # If no tool calls, the turn is complete.
            if not tool_calls:
                await ws.send_json({"type": "done"})
                return

            # ---- Execute each tool call ----------------------------------
            for tc in tool_calls:
                name = tc["name"]
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}

                tool_counter += 1
                call_id = f"tc-{tool_counter}"

                # --- Approval gate ----------------------------------------
                if needs_approval(name, args):
                    loop = asyncio.get_running_loop()
                    pending_approval.clear()
                    pending_approval.update(id=call_id, future=loop.create_future())
                    await ws.send_json(
                        {
                            "type": "approval_required",
                            "id": call_id,
                            "tool": name,
                            "path": args.get("path", ""),
                            "namespace": args.get("namespace") or "(from the manifests)",
                        }
                    )
                    try:
                        approved = await pending_approval["future"]
                    finally:
                        pending_approval.clear()

                    if not approved:
                        messages.append(
                            {"role": "tool", "tool_call_id": tc["id"], "content": DECLINED_MESSAGE}
                        )
                        await ws.send_json(
                            {"type": "tool_start", "id": call_id, "name": name, "arguments": args}
                        )
                        await ws.send_json(
                            {
                                "type": "tool_end",
                                "id": call_id,
                                "name": name,
                                "status": "declined",
                                "result_length": len(DECLINED_MESSAGE),
                                "result_preview": DECLINED_MESSAGE,
                            }
                        )
                        continue
                    args = approved_arguments(args)

                # --- Run the tool -----------------------------------------
                await ws.send_json(
                    {"type": "tool_start", "id": call_id, "name": name, "arguments": args}
                )

                status, text = await bridge.call_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": text})

                preview = text[:500] + "…" if len(text) > 500 else text
                await ws.send_json(
                    {
                        "type": "tool_end",
                        "id": call_id,
                        "name": name,
                        "status": status,
                        "result_length": len(text),
                        "result_preview": preview,
                    }
                )

        # Step limit reached.
        await ws.send_json({"type": "done"})

    async def run_chat_safe(prompt: str) -> None:
        try:
            await run_chat(prompt)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - reported to the browser
            logger.exception("Chat error")
            try:
                await ws.send_json({"type": "error", "message": str(exc)})
                await ws.send_json({"type": "done"})
            except Exception:  # noqa: BLE001
                pass  # WebSocket already closed.

    def busy() -> bool:
        return chat_task is not None and not chat_task.done()

    # ------------------------------------------------------------------
    # Send initial connection info.
    # ------------------------------------------------------------------
    await ws.send_json(
        {
            "type": "connected",
            "server_name": bridge.server_name,
            "model": model,
            "tools": bridge.tools_info,
        }
    )

    # ------------------------------------------------------------------
    # Read messages from the browser.
    # ------------------------------------------------------------------
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue

            kind = data.get("type")

            if kind == "chat":
                prompt = data.get("message")
                if not isinstance(prompt, str) or not prompt.strip():
                    continue
                if busy():
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "Please wait for the current response to finish.",
                        }
                    )
                    continue
                chat_task = asyncio.create_task(run_chat_safe(prompt[:MAX_PROMPT_CHARS]))

            elif kind == "approve":
                future = pending_approval.get("future")
                # Only the click for the pending call counts, and only a real boolean yes.
                if future and not future.done() and data.get("id") == pending_approval.get("id"):
                    future.set_result(data.get("approved") is True)

            elif kind == "clear":
                if busy():
                    await ws.send_json(
                        {"type": "error", "message": "Wait for the current response before clearing."}
                    )
                    continue
                messages[:] = [messages[0]]  # Keep system prompt.
                await ws.send_json({"type": "cleared"})

        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
            break

    if chat_task and not chat_task.done():
        chat_task.cancel()

    return ws


# ═══════════════════════════════════════════════════════════════════════════
# Static file handler
# ═══════════════════════════════════════════════════════════════════════════


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(Path(__file__).parent / "index.html")


# ═══════════════════════════════════════════════════════════════════════════
# Application lifecycle
# ═══════════════════════════════════════════════════════════════════════════


async def on_startup(app: web.Application) -> None:
    stack = AsyncExitStack()
    app["stack"] = stack
    await stack.__aenter__()

    settings = get_settings()
    configure_logging(settings)

    if not settings.openai_api_key:
        bridge.startup_error = "OPENAI_API_KEY is not set. Add it to .env and restart the web client."
        logger.error(bridge.startup_error)
        return

    try:
        await bridge.connect(stack)
        logger.info(
            "Connected to MCP server '%s' with %d tools",
            bridge.server_name,
            len(bridge.tools_info),
        )
    except Exception:  # noqa: BLE001 - reported to the browser
        bridge.startup_error = (
            f"Could not connect to the MCP server at {MCP_URL}. Is it running (run.bat)? "
            "If it requires a token, set the same MCP_AUTH_TOKEN for both."
        )
        logger.exception(bridge.startup_error)


async def on_cleanup(app: web.Application) -> None:
    stack: AsyncExitStack | None = app.get("stack")
    if stack:
        await stack.__aexit__(None, None, None)


def main() -> None:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_websocket)

    print()
    print("  +----------------------------------------------+")
    print("  |   docker2k8s Web Client                      |")
    print(f"  |   Open  http://localhost:{WEB_PORT}  in your browser  |")
    print("  +----------------------------------------------+")
    print()

    web.run_app(app, host=WEB_HOST, port=WEB_PORT, print=None)


if __name__ == "__main__":
    main()

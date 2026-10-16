"""Reference LLM agent for the docker2k8s MCP server.

This is one client among many -- the server is a standard MCP server and works
with Claude Desktop, Claude Code, Cursor, VS Code or anything else that speaks
MCP.  This agent exists to demonstrate the full loop end to end:

    user request -> model picks a tool -> tool result -> model reasons
    -> next tool -> ... -> answer

The model chooses the tools.  Nothing here hardcodes "call A then B then C".
The one thing the client does enforce is the human approval gate before a
deployment: the server refuses unapproved applies, and this client refuses to
set approved=true without asking the person at the keyboard first.

Usage:
    python -m docker2k8s_mcp.client.agent "Migrate examples/fastapi-mysql to Kubernetes."
    python -m docker2k8s_mcp.client.agent --http http://127.0.0.1:8000/mcp "..."
    python -m docker2k8s_mcp.client.agent            # interactive
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import configure_logging, get_settings
from .common import (
    DECLINED_MESSAGE,
    SYSTEM_PROMPT,
    approved_arguments,
    connect_http,
    needs_approval,
    openai_tools,
    result_text,
)

logger = logging.getLogger("agent")

MAX_STEPS = 40


class MigrationAgent:
    """An OpenAI-driven agent that drives the docker2k8s MCP tools."""

    def __init__(self, session: ClientSession, model: str, auto_approve: bool = False) -> None:
        self.session = session
        self.model = model
        self.auto_approve = auto_approve
        self.messages: list[dict[str, Any]] = []
        self._tools: list[dict[str, Any]] = []
        self._client: Any = None

    async def start(self) -> None:
        """Initialize the MCP session and translate its tools for the model."""
        init = await self.session.initialize()
        system = SYSTEM_PROMPT
        if init.instructions:
            system += f"\n\n--- Server instructions ---\n{init.instructions}"
        self.messages = [{"role": "system", "content": system}]
        self._tools = openai_tools((await self.session.list_tools()).tools)
        print(f"Connected to '{init.server_info.name}' with {len(self._tools)} tools.\n")

    def _openai(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI  # imported late so the server needs no openai

            settings = get_settings()
            if not settings.openai_api_key:
                raise SystemExit(
                    "OPENAI_API_KEY is not set. Put it in .env or the environment.\n"
                    "The MCP server itself does not need it -- only this reference client does."
                )
            self._client = AsyncOpenAI(
                api_key=settings.openai_api_key, base_url=settings.openai_base_url
            )
        return self._client

    async def ask(self, prompt: str) -> str:
        """Run one user turn to completion, calling tools as the model decides."""
        client = self._openai()
        self.messages.append({"role": "user", "content": prompt})

        for _ in range(MAX_STEPS):
            response = await client.chat.completions.create(
                model=self.model,
                messages=self.messages,  # type: ignore[arg-type]
                tools=self._tools,  # type: ignore[arg-type]
                tool_choice="auto",
            )
            message = response.choices[0].message
            self.messages.append(message.model_dump(exclude_none=True))

            if not message.tool_calls:
                return message.content or ""

            for call in message.tool_calls:
                result = await self._run_tool(call.function.name, call.function.arguments)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        return "Reached the step limit without finishing. Ask me to continue."

    async def _run_tool(self, name: str, raw_arguments: str) -> str:
        """Execute one MCP tool call, enforcing the approval gate first."""
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return f"Error: could not parse tool arguments as JSON: {exc}"
        if not isinstance(arguments, dict):
            return "Error: tool arguments must be a JSON object."

        if needs_approval(name, arguments):
            if not await self._confirm(name, arguments):
                return DECLINED_MESSAGE
            arguments = approved_arguments(arguments)

        print(f"  -> {name}({_short(arguments)})")
        try:
            result = await self.session.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not fatal
            return f"Tool call failed: {exc}"

        text = result_text(result)
        print(f"     <- {'error' if result.is_error else 'ok'}, {len(text)} chars")
        return text

    async def _confirm(self, name: str, arguments: dict[str, Any]) -> bool:
        """Ask the human before anything is written to the cluster."""
        if self.auto_approve:
            print(f"  !! auto-approving {name} (--yes)")
            return True
        print("\n" + "=" * 68)
        print(f"APPROVAL REQUIRED: {name}")
        print(f"  path:      {arguments.get('path')}")
        print(f"  namespace: {arguments.get('namespace') or '(from the manifests)'}")
        print("This will change workloads in your Kubernetes cluster.")
        print("=" * 68)
        try:
            # input() blocks; keep the MCP session's event loop responsive meanwhile.
            answer = (await asyncio.to_thread(input, "Deploy? [y/N] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in {"y", "yes"}


def _short(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def _connect(stack: AsyncExitStack, http_url: str | None) -> ClientSession:
    """Open an MCP session over stdio (default) or streamable HTTP."""
    if http_url:
        return await connect_http(stack, http_url, get_settings().mcp_auth_token)
    params = StdioServerParameters(command=sys.executable, args=["-m", "docker2k8s_mcp.main"])
    read, write = await stack.enter_async_context(stdio_client(params))
    return await stack.enter_async_context(ClientSession(read, write))


async def run(prompt: str | None, http_url: str | None, model: str, auto_approve: bool) -> int:
    async with AsyncExitStack() as stack:
        session = await _connect(stack, http_url)
        agent = MigrationAgent(session, model=model, auto_approve=auto_approve)
        await agent.start()

        if prompt:
            print(await agent.ask(prompt))
            return 0

        print("Interactive mode. Type 'exit' to quit.\n")
        while True:
            try:
                line = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if line.lower() in {"exit", "quit"}:
                return 0
            if not line:
                continue
            print("\n" + await agent.ask(line) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="docker2k8s-agent",
        description="Reference LLM agent for the docker2k8s MCP server.",
    )
    parser.add_argument("prompt", nargs="?", help="One-shot request. Omit for interactive mode.")
    parser.add_argument(
        "--http",
        metavar="URL",
        help="Connect to an already-running server over streamable HTTP, "
        "e.g. http://127.0.0.1:8000/mcp. Sends MCP_AUTH_TOKEN when set. "
        "Default is to launch the server over stdio.",
    )
    parser.add_argument("--model", default=None, help="Override OPENAI_MODEL.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive deployment confirmation. Use only in automation.",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    settings = get_settings()
    configure_logging(settings)
    return asyncio.run(
        run(args.prompt, args.http, args.model or settings.openai_model, args.yes)
    )


if __name__ == "__main__":
    raise SystemExit(main())

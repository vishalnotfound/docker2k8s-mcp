"""Pieces shared by the reference CLI agent and the web client."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

SYSTEM_PROMPT = """\
You are a Kubernetes migration engineer with access to the docker2k8s MCP tools.

Follow the server's instructions for the migration workflow. Decide which tool to
call next based on what you have learned, not a fixed script.

Non-negotiable rules:
- Always validate manifests before deploying. If validation fails, explain why and
  fix the cause instead of deploying.
- Never deploy without the user's explicit approval in this conversation. Present
  the migration plan, its blockers and warnings and the validation result, then ask.
- Never claim a deployment succeeded without calling verify_deployment.
- If verification fails, diagnose it, read the relevant logs and events, and
  explain the cause and the fix.
- Secret values are never shown to you. Generated Secrets hold placeholders; tell
  the user they must fill them in.

Be concise. Report what you found and what you did, not a narration of every call.
"""

#: Tools that change the cluster and therefore need a human yes.
APPROVAL_TOOLS = frozenset({"apply_manifests"})

#: Tool output kept in the model's history. Longer results are cut so a long
#: session does not grow without bound, in memory or in tokens.
MAX_TOOL_RESULT_CHARS = 20_000

DECLINED_MESSAGE = (
    "The user DECLINED the deployment. Do not attempt to deploy again unless they "
    "explicitly ask. Summarise what is ready instead."
)


def needs_approval(name: str, arguments: dict[str, Any]) -> bool:
    """True when a call could write to the cluster.

    Only a literal ``dry_run: true`` counts as a dry run: the server coerces
    "false", "no", "0" and friends to False, so a truthiness check here would
    let a string skip the human and deploy for real.
    """
    return name in APPROVAL_TOOLS and bool(arguments.get("approved")) and arguments.get("dry_run") is not True


def approved_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """The exact arguments a human approved: a real, approved deployment."""
    return {**arguments, "approved": True, "dry_run": False}


def openai_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """Translate MCP tool definitions into OpenAI function-calling schemas."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or tool.title or tool.name,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def result_text(result: Any) -> str:
    """A tool result as text for the model, truncated to MAX_TOOL_RESULT_CHARS."""
    payload = result.structured_content
    if payload is None:
        payload = "\n".join(block.text for block in result.content if getattr(block, "text", None))
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = (
            text[:MAX_TOOL_RESULT_CHARS]
            + f"\n...[truncated {len(text) - MAX_TOOL_RESULT_CHARS} characters]"
        )
    return text


async def connect_http(stack: AsyncExitStack, url: str, token: str | None) -> ClientSession:
    """Open an MCP session over streamable HTTP, sending the bearer token if set."""
    http_client = None
    if token:
        from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: PLC0415

        http_client = await stack.enter_async_context(
            create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        )
    streams = await stack.enter_async_context(streamable_http_client(url, http_client=http_client))
    return await stack.enter_async_context(ClientSession(streams[0], streams[1]))

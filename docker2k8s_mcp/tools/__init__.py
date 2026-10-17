"""MCP tool registration.

Tools are thin: they validate nothing beyond what the MCP schema already does,
call a service, and translate domain errors into the MCP error shape.  All the
logic lives in ``src/services``, ``src/generators`` and ``src/validators``.
"""

from __future__ import annotations

import functools
import inspect
import logging
import traceback
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from ..errors import D2KError

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def mcp_errors(func: F) -> F:
    """Turn a ``D2KError`` into a ToolError the model can act on.

    A ToolError reaches the model as a readable message with no traceback, while
    anything unexpected is left to the SDK, which tells the model only that the
    tool failed and logs the traceback server-side.  Tracebacks are added to the
    message only when DEBUG is enabled.
    """

    def _to_tool_error(exc: D2KError) -> ToolError:
        from ..config import get_settings

        logger.warning("%s: %s", exc.code, exc.message)
        message = f"{exc.code}: {exc.message}"
        if exc.hint:
            message += f"\nWhat to try: {exc.hint}"
        if get_settings().debug:
            message += f"\n\n{traceback.format_exc()}"
        return ToolError(message)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except D2KError as exc:
                raise _to_tool_error(exc) from exc

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except D2KError as exc:
            raise _to_tool_error(exc) from exc

    return wrapper  # type: ignore[return-value]


__all__ = ["mcp_errors"]

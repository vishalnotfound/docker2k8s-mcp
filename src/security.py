"""Path sandboxing and secret detection.

Two jobs, both about not leaking things we shouldn't:

* ``resolve_project_path`` keeps file access inside configured roots so an MCP
  client cannot walk the whole filesystem.
* ``is_secret_key`` / ``redact`` keep credential *values* out of tool output and
  out of the logs.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import get_settings
from .errors import PathNotAllowed, ProjectNotFound

REDACTED = "<REDACTED>"
REQUIRED_SECRET = "<REQUIRED_SECRET>"

#: Substrings that mark an environment variable as sensitive.
_SECRET_MARKERS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
    "auth",
    "salt",
    "cert",
    "signing",
)

#: Keys that contain "key"/"auth" but are almost never secret.
_SECRET_ALLOWLIST = (
    "key_file",
    "keyspace",
    "keep_alive",
    "auth_type",
    "authority_url",
    "public_key",
)

#: Connection strings frequently embed a password (postgres://user:pw@host).
_URL_WITH_CREDENTIALS = re.compile(r"^[a-z0-9+.-]+://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE)


def is_secret_key(key: str) -> bool:
    """Heuristically decide whether an env var name holds a credential."""
    lowered = key.lower()
    if any(allowed in lowered for allowed in _SECRET_ALLOWLIST):
        return False
    return any(marker in lowered for marker in _SECRET_MARKERS)


def is_secret_value(value: str) -> bool:
    """True when the *value* looks like a credential even if the key is bland."""
    return bool(_URL_WITH_CREDENTIALS.match(value.strip()))


def classify_env(key: str, value: str | None) -> bool:
    """Return True when ``key=value`` should be treated as a Kubernetes Secret."""
    if is_secret_key(key):
        return True
    return bool(value) and is_secret_value(value)


def redact(key: str, value: str | None) -> str | None:
    """Return a value safe to hand back to an LLM."""
    if value is None:
        return None
    if classify_env(key, value):
        return REDACTED
    return value


def resolve_project_path(path: str | Path, *, must_exist: bool = True) -> Path:
    """Resolve ``path`` and assert it lives under an allowed root.

    Raises:
        PathNotAllowed: the resolved path escapes every configured root.
        ProjectNotFound: ``must_exist`` is set and nothing is there.
    """
    settings = get_settings()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # resolve() collapses '..' so traversal is caught by the containment check.
    resolved = candidate.resolve()

    roots = settings.allowed_root_paths
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise PathNotAllowed(
            f"Path '{resolved}' is outside the allowed roots.",
            hint=(
                "Allowed roots: "
                + ", ".join(str(r) for r in roots)
                + ". Set ALLOWED_ROOTS in the server environment to widen access."
            ),
        )

    if must_exist and not resolved.exists():
        raise ProjectNotFound(
            f"No such file or directory: {resolved}",
            hint="Check the path. Paths are resolved relative to the MCP server's working directory.",
        )
    return resolved

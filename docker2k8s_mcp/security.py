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

#: Substrings that mark an environment variable as sensitive wherever they appear.
_SECRET_SUBSTRINGS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "api_key",
    "access_key",
    "private_key",
    "credential",
    "signing",
    "webhook",
)

#: Short markers that only count as a whole word (DB_PASS, but not PASSENGER_COUNT).
_SECRET_WORDS = frozenset({"pass", "pwd", "auth", "dsn", "jwt", "salt"})

#: Exact names that contain a marker but are never secret.
_NOT_SECRET = frozenset({"pwd", "oldpwd"})

#: Name fragments that contain a marker but are almost never secret.
_SECRET_ALLOWLIST = (
    "key_file",
    "key_path",
    "keyspace",
    "auth_type",
    "authority_url",
    "public_key",
    "publishable_key",
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")

#: Connection strings frequently embed a password: postgres://user:pw@host, and
#: redis://:pw@host with an empty user name.
_URL_WITH_CREDENTIALS = re.compile(r"^[a-z0-9+.-]+://[^/\s:@]*:[^/\s@]+@", re.IGNORECASE)


def is_secret_key(key: str) -> bool:
    """Heuristically decide whether an env var name holds a credential.

    Errs on the side of calling something a secret: a false positive only means
    a value goes into a Secret with a placeholder, while a false negative writes
    a credential into a ConfigMap.
    """
    lowered = _CAMEL_BOUNDARY.sub("_", key).lower()
    if lowered in _NOT_SECRET or any(allowed in lowered for allowed in _SECRET_ALLOWLIST):
        return False
    if any(marker in lowered for marker in _SECRET_SUBSTRINGS):
        return True
    words = [w for w in _WORD_SPLIT.split(lowered) if w]
    # STRIPE_KEY, APP_KEY, ENCRYPTION_KEY: a name ending in "key" is a key.
    return bool(words) and (words[-1] in {"key", "keys"} or any(w in _SECRET_WORDS for w in words))


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


def resolve_project_path(
    path: str | Path, *, must_exist: bool = True, base: Path | None = None
) -> Path:
    """Resolve ``path`` and assert it lives under an allowed root.

    Relative paths are resolved against ``base`` (the working directory by default).

    Raises:
        PathNotAllowed: the resolved path escapes every configured root.
        ProjectNotFound: ``must_exist`` is set and nothing is there.
    """
    settings = get_settings()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
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

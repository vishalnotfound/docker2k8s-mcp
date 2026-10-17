"""Domain errors for docker2k8s.

Errors carry a machine-readable ``code``, a human message and (optionally) a
``hint`` telling the LLM what it can try next.  Stack traces are only surfaced
when ``DEBUG`` is enabled in the configuration.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class D2KError(Exception):
    """Base class for all expected docker2k8s failures."""

    code = "Error"

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        return payload


class ProjectNotFound(D2KError):
    code = "ProjectNotFound"


class DockerfileNotFound(D2KError):
    code = "DockerfileNotFound"


class ComposeFileNotFound(D2KError):
    code = "ComposeFileNotFound"


class InvalidComposeFile(D2KError):
    code = "InvalidComposeFile"


class InvalidManifest(D2KError):
    code = "InvalidManifest"


class ValidationFailed(D2KError):
    code = "ValidationError"


class KubernetesConnectionError(D2KError):
    code = "KubernetesConnectionError"


class KubernetesNotFound(D2KError):
    code = "KubernetesNotFound"


class DeploymentFailed(D2KError):
    code = "DeploymentFailed"


class ApprovalRequired(D2KError):
    code = "ApprovalRequired"


class PathNotAllowed(D2KError):
    code = "PermissionError"

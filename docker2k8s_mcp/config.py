"""Configuration loaded from environment variables / .env."""

from __future__ import annotations

import functools
import logging
import os
import sys
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _is_too_broad(path: Path) -> bool:
    """A filesystem root, the home directory or one of its ancestors.

    MCP clients often launch servers from one of these, and sandboxing to it
    would expose far more than the user's projects.
    """
    home = Path.home().resolve()
    return path == Path(path.anchor) or path == home or path in home.parents


class Settings(BaseSettings):
    """Runtime settings.  Every value can be overridden by an env var."""

    model_config = SettingsConfigDict(
        # Anchored to the checkout, not the working directory: MCP clients start
        # the server from arbitrary directories, which may hold some other
        # project's .env.
        env_file=_PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM (only used by the bundled reference client, not by the server) ---
    openai_api_key: str | None = Field(default=None)
    openai_model: str = Field(default="gpt-4o")
    openai_base_url: str | None = Field(default=None)

    # --- Tooling ---
    kubectl_path: str = Field(default="kubectl")
    kube_context: str | None = Field(default=None)
    kube_namespace: str = Field(default="default")

    # --- Filesystem ---
    generated_dir: Path = Field(default=_PROJECT_ROOT / "generated")
    allowed_roots: str = Field(
        default="",
        description=(
            "OS-path-separator delimited list of directories the server may read from. "
            "Defaults to the current working directory and the project root."
        ),
    )

    # --- Behaviour ---
    log_level: str = Field(default="INFO")
    debug: bool = Field(default=False)
    kubectl_timeout: int = Field(default=120, description="Seconds before a kubectl call is aborted.")
    mcp_auth_token: str | None = Field(
        default=None,
        description="Bearer token required by the http/sse transports. Mandatory off-loopback.",
    )

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        value = value.upper()
        if value not in LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
        return value

    @field_validator("generated_dir")
    @classmethod
    def _anchor(cls, value: Path) -> Path:
        # './generated' in .env means the checkout's folder, wherever we were launched from.
        value = value.expanduser()
        return value if value.is_absolute() else _PROJECT_ROOT / value

    @property
    def allowed_root_paths(self) -> list[Path]:
        """Directories the server is allowed to read project files from."""
        roots: list[Path] = []
        raw = self.allowed_roots.strip()
        if raw:
            for part in raw.split(os.pathsep):
                part = part.strip()
                if part:
                    path = Path(part).expanduser()
                    roots.append((path if path.is_absolute() else _PROJECT_ROOT / path).resolve())
        else:
            cwd = Path.cwd().resolve()
            if not _is_too_broad(cwd):
                roots.append(cwd)
            roots.append(_PROJECT_ROOT)
        roots.append(self.generated_dir.resolve())
        # de-duplicate, keep order
        seen: set[Path] = set()
        unique: list[Path] = []
        for root in roots:
            if root not in seen:
                seen.add(root)
                unique.append(root)
        return unique


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def configure_logging(settings: Settings | None = None) -> None:
    """Set up logging on stderr.

    stdout is reserved for the MCP stdio transport, so logs must never go there.
    """
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    # The kubernetes client is chatty at DEBUG level.
    logging.getLogger("kubernetes").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

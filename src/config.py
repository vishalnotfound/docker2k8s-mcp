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


class Settings(BaseSettings):
    """Runtime settings.  Every value can be overridden by an env var."""

    model_config = SettingsConfigDict(
        env_file=".env",
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

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def allowed_root_paths(self) -> list[Path]:
        """Directories the server is allowed to read project files from."""
        roots: list[Path] = []
        raw = self.allowed_roots.strip()
        if raw:
            for part in raw.split(os.pathsep):
                part = part.strip()
                if part:
                    roots.append(Path(part).expanduser().resolve())
        else:
            roots.append(Path.cwd().resolve())
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

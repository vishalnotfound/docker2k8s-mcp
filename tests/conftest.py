"""Shared fixtures.

Unit tests build small Docker projects on disk in a tmp_path; none of them needs
a Docker daemon or a Kubernetes cluster.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_settings  # noqa: E402

COMPOSE = """\
services:
  api:
    build:
      context: .
      dockerfile: Dockerfile
    image: demo-api:local
    ports:
      - "8000:8000"
    environment:
      APP_PORT: 8000
      LOG_LEVEL: INFO
      DB_HOST: db
      DB_PASSWORD: ${DB_PASSWORD}
    depends_on:
      - db
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 3s
      retries: 3
      start_period: 20s
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: "0.5"
          memory: 512M

  db:
    image: postgres:16
    ports:
      - "127.0.0.1:5432:5432"
    environment:
      - POSTGRES_PASSWORD=secret
      - POSTGRES_DB=demo
    volumes:
      - db-data:/var/lib/postgresql/data
      - ./initdb:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s

volumes:
  db-data:

networks:
  default:
    driver: bridge
"""

DOCKERFILE = """\
FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim
ENV APP_PORT=8000 \\
    LOG_LEVEL=INFO
ARG BUILD_REV
WORKDIR /app
COPY app ./app
USER appuser
EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0"]
"""

ENV_FILE = """\
# comment line
DB_PASSWORD=super-secret
POSTGRES_DB=demo
LOG_LEVEL=DEBUG
export API_TOKEN="tok-123"
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small but realistic Docker project inside the allowed roots."""
    root = tmp_path / "demo-app"
    (root / "app").mkdir(parents=True)
    (root / "initdb").mkdir()

    (root / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    (root / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    (root / ".env").write_text(ENV_FILE, encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (root / "app" / "main.py").write_text("app = None\n", encoding="utf-8")

    _allow(monkeypatch, tmp_path)
    return root


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory that the path sandbox permits."""
    _allow(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    """Settings are cached for the process; tests must not inherit each other's."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _allow(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point the path sandbox and the generated dir at the test's tmp_path."""
    monkeypatch.setenv("ALLOWED_ROOTS", str(root))
    monkeypatch.setenv("GENERATED_DIR", str(root / "generated"))
    get_settings.cache_clear()

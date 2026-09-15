"""Tests for project detection, Dockerfile parsing and Compose parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.errors import ComposeFileNotFound, InvalidComposeFile, PathNotAllowed, ProjectNotFound
from src.services import docker_service


# --------------------------------------------------------------------------
# Project detection
# --------------------------------------------------------------------------
def test_project_detection(project: Path) -> None:
    result = docker_service.inspect_project(str(project))

    assert result.dockerfile is True
    assert result.dockerfile_paths == ["Dockerfile"]
    assert result.compose_file is True
    assert result.compose_path is not None
    assert result.env_file is True
    assert ".env" in result.env_files
    assert result.services == ["api", "db"]
    assert result.source_dirs == ["app"]
    assert result.dependency_files == ["requirements.txt"]
    assert result.project_name == "demo-app"


def test_project_detection_rejects_missing_directory(sandbox: Path) -> None:
    with pytest.raises(ProjectNotFound):
        docker_service.inspect_project(str(sandbox / "nope"))


def test_project_detection_blocks_paths_outside_allowed_roots(project: Path) -> None:
    # '..' is collapsed before the containment check, so traversal cannot escape.
    with pytest.raises(PathNotAllowed):
        docker_service.inspect_project(str(project / ".." / ".." / ".." / ".."))


def test_project_without_compose_still_reports_dockerfile(sandbox: Path) -> None:
    root = sandbox / "solo"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")

    result = docker_service.inspect_project(str(root))

    assert result.dockerfile is True
    assert result.compose_file is False
    assert result.services == []
    assert any("No Compose file" in note for note in result.notes)


# --------------------------------------------------------------------------
# Dockerfile parsing
# --------------------------------------------------------------------------
def test_dockerfile_parsing(project: Path) -> None:
    result = docker_service.inspect_dockerfile(str(project))

    assert result.multi_stage is True
    assert [s.name for s in result.stages] == ["builder", None]
    assert result.final_base_image == "python:3.12-slim"
    assert result.exposed_ports == [8000]
    assert result.workdir == "/app"
    assert result.user == "appuser"
    assert result.cmd == ["uvicorn", "app.main:app", "--host", "0.0.0.0"]
    assert result.build_args == ["BUILD_REV"]
    assert result.healthcheck is not None


def test_dockerfile_parsing_joins_line_continuations(project: Path) -> None:
    # ENV APP_PORT=8000 \<newline> LOG_LEVEL=INFO is one instruction.
    result = docker_service.inspect_dockerfile(str(project))

    assert result.env_keys == ["APP_PORT", "LOG_LEVEL"]


def test_dockerfile_parsing_notes_root_user(sandbox: Path) -> None:
    path = sandbox / "Dockerfile"
    path.write_text("FROM alpine:3.20\nCMD sh\n", encoding="utf-8")

    result = docker_service.inspect_dockerfile(str(path))

    assert result.user is None
    assert any("runs as root" in note for note in result.notes)
    assert any("No EXPOSE" in note for note in result.notes)


# --------------------------------------------------------------------------
# Compose parsing
# --------------------------------------------------------------------------
def test_compose_parsing(project: Path) -> None:
    result = docker_service.inspect_compose(str(project))

    assert [s.name for s in result.services] == ["api", "db"]
    assert result.named_volumes == ["db-data"]
    assert result.networks == ["default"]

    api = result.services[0]
    assert api.image == "demo-api:local"
    assert api.build_context == "."
    assert api.replicas == 2
    assert api.depends_on == ["db"]
    assert api.resources is not None
    # Compose's '512M' is not a valid Kubernetes quantity; it becomes '512Mi'.
    assert api.resources.memory_limit == "512Mi"
    assert api.resources.cpu_limit == "0.5"


def test_compose_parsing_normalises_port_syntaxes(project: Path) -> None:
    result = docker_service.inspect_compose(str(project))
    api, db = result.services

    assert [(p.host_port, p.container_port) for p in api.ports] == [(8000, 8000)]
    # "127.0.0.1:5432:5432" must not have its published port eaten by the host IP.
    assert [(p.host_port, p.container_port) for p in db.ports] == [(5432, 5432)]


def test_compose_parsing_accepts_list_and_mapping_environment(project: Path) -> None:
    result = docker_service.inspect_compose(str(project))
    api, db = result.services

    assert api.environment["APP_PORT"] == "8000"  # mapping syntax
    assert db.environment["POSTGRES_DB"] == "demo"  # list syntax


def test_compose_parsing_classifies_volumes(project: Path) -> None:
    result = docker_service.inspect_compose(str(project))
    db = result.services[1]

    kinds = {v.target: v.kind for v in db.volumes}
    assert kinds["/var/lib/postgresql/data"] == "named"
    assert kinds["/docker-entrypoint-initdb.d"] == "bind"
    assert any("bind mount" in w for w in result.warnings)


def test_compose_parsing_converts_healthcheck_durations(project: Path) -> None:
    result = docker_service.inspect_compose(str(project))
    api = result.services[0]

    assert api.healthcheck is not None
    assert api.healthcheck.interval_seconds == 10
    assert api.healthcheck.timeout_seconds == 3
    assert api.healthcheck.start_period_seconds == 20


def test_compose_parsing_detects_stateful_services(project: Path) -> None:
    result = docker_service.inspect_compose(str(project))
    api, db = result.services

    assert db.stateful is True
    # 'demo-api' is built locally and is not a datastore, despite the volume-less image.
    assert api.stateful is False


def test_compose_parsing_rejects_invalid_yaml(sandbox: Path) -> None:
    bad = sandbox / "docker-compose.yml"
    bad.write_text("services:\n  api:\n   - broken: [\n", encoding="utf-8")

    with pytest.raises(InvalidComposeFile):
        docker_service.inspect_compose(str(bad))


def test_compose_parsing_rejects_file_without_services(sandbox: Path) -> None:
    bad = sandbox / "compose.yaml"
    bad.write_text("version: '3'\n", encoding="utf-8")

    with pytest.raises(InvalidComposeFile):
        docker_service.inspect_compose(str(bad))


def test_missing_compose_file_raises(sandbox: Path) -> None:
    with pytest.raises(ComposeFileNotFound):
        docker_service.inspect_compose(str(sandbox))


# --------------------------------------------------------------------------
# Environment / secrets
# --------------------------------------------------------------------------
def test_environment_never_returns_secret_values(project: Path) -> None:
    result = docker_service.inspect_environment(str(project))

    by_key = {v.key: v for v in result.variables}
    assert by_key["DB_PASSWORD"].is_secret is True
    assert by_key["DB_PASSWORD"].value is None
    assert by_key["API_TOKEN"].is_secret is True
    assert by_key["API_TOKEN"].value is None
    # Non-secret values are safe to return and useful for the plan.
    assert by_key["LOG_LEVEL"].value == "DEBUG"
    # DB_PASSWORD and API_TOKEN from .env, POSTGRES_PASSWORD from Compose.
    assert by_key["POSTGRES_PASSWORD"].is_secret is True
    assert result.secret_count == 3


def test_compose_redacts_secret_values(project: Path) -> None:
    result = docker_service.inspect_compose(str(project))
    db = result.services[1]

    assert "POSTGRES_PASSWORD" in db.secret_keys
    assert db.environment["POSTGRES_PASSWORD"] == "<REDACTED>"
    assert "secret" not in str(db.environment)

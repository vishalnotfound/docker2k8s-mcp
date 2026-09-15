"""Tests for migration analysis and plan construction."""

from __future__ import annotations

from pathlib import Path

from src.security import REQUIRED_SECRET
from src.services import migration_service


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def test_service_analysis(project: Path) -> None:
    analysis = migration_service.analyze_project(str(project))

    assert analysis.service_count == 2
    assert analysis.services == ["api", "db"]
    assert analysis.stateful_services == ["db"]
    assert analysis.blockers == []

    kinds = {(m.service, m.kubernetes_concept) for m in analysis.mappings}
    assert ("api", "Deployment") in kinds
    assert ("db", "StatefulSet") in kinds


def test_service_analysis_maps_docker_concepts_to_kubernetes(project: Path) -> None:
    analysis = migration_service.analyze_project(str(project))
    concepts = {m.kubernetes_concept for m in analysis.mappings}

    assert "ConfigMap" in concepts
    assert "Secret" in concepts
    assert "PersistentVolumeClaim" in concepts
    assert "readinessProbe + livenessProbe" in concepts
    assert "Service DNS + NetworkPolicy" in concepts


def test_service_analysis_warns_about_unmigratable_concepts(project: Path) -> None:
    analysis = migration_service.analyze_project(str(project))
    codes = {w.code for w in analysis.warnings}

    assert "BIND_MOUNT" in codes  # ./initdb has no Kubernetes equivalent
    assert "DEPENDS_ON" in codes  # Kubernetes does not order pod startup
    assert "SECRETS_DETECTED" in codes
    assert "LOCAL_BUILD" in codes  # the cluster cannot build images

    bind = next(w for w in analysis.warnings if w.code == "BIND_MOUNT")
    assert bind.suggestion  # a warning without a suggested action is not useful


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------
def test_migration_plan(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))

    assert plan.project_name == "demo-app"
    assert plan.namespace == "default"
    assert [s.name for s in plan.services] == ["api", "db"]

    assert "Deployment/api" in plan.resources
    assert "StatefulSet/db" in plan.resources
    assert "Service/api" in plan.resources
    assert "ConfigMap/api-config" in plan.resources
    assert "Secret/api-secret" in plan.resources
    assert plan.manual_steps  # a local build always needs a human step


def test_migration_plan_chooses_workload_kind(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))
    api, db = plan.services

    assert api.workload_kind == "Deployment"
    assert api.replicas == 2
    assert db.workload_kind == "StatefulSet"
    assert db.headless is True
    # A datastore is scaled back to one replica: clustering is not automatic.
    assert db.replicas == 1


def test_port_mapping(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))
    api, db = plan.services

    assert [(p.port, p.target_port) for p in api.service_ports] == [(8000, 8000)]
    assert [p.container_port for p in api.container_ports] == [8000]
    # Compose published 8000, which is outside the NodePort range, so no nodePort.
    assert api.service_type == "NodePort"
    assert api.service_ports[0].node_port is None
    assert any("port-forward" in step for step in plan.manual_steps)

    # A published database port stays internal in a cluster.
    assert db.service_type == "ClusterIP"


def test_port_mapping_uses_dockerfile_expose_as_fallback(sandbox: Path) -> None:
    root = sandbox / "noports"
    root.mkdir()
    (root / "docker-compose.yml").write_text(
        "services:\n  web:\n    build: .\n", encoding="utf-8"
    )
    (root / "Dockerfile").write_text("FROM alpine\nEXPOSE 9000\n", encoding="utf-8")

    plan = migration_service.create_migration_plan(str(root))

    assert [p.container_port for p in plan.services[0].container_ports] == [9000]
    assert any(w.code == "PORT_FROM_DOCKERFILE" for w in plan.warnings)


def test_volume_detection(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))
    db = plan.services[1]

    assert [v.name for v in db.volumes] == ["db-data"]
    assert db.volumes[0].mount_path == "/var/lib/postgresql/data"
    assert db.volumes[0].source_kind == "named"
    # The bind mount is not silently dropped; it becomes a human decision.
    assert any("initdb" in step for step in plan.manual_steps)


def test_secret_detection(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))

    assert "DB_PASSWORD" in plan.secrets
    assert "POSTGRES_PASSWORD" in plan.secrets
    assert "LOG_LEVEL" in plan.environment_variables
    assert "DB_PASSWORD" not in plan.environment_variables

    for service in plan.services:
        for env in service.env:
            if env.is_secret:
                # A real credential must never reach the plan.
                assert env.value == REQUIRED_SECRET
    assert "super-secret" not in plan.model_dump_json()


def test_secret_detection_promotes_interpolated_credentials(sandbox: Path) -> None:
    """A bland key holding a credential URL is still a secret."""
    root = sandbox / "urlapp"
    root.mkdir()
    (root / "docker-compose.yml").write_text(
        "services:\n"
        "  api:\n"
        "    image: demo:1\n"
        "    environment:\n"
        "      DATABASE_URL: postgres://user:hunter2@db:5432/app\n",
        encoding="utf-8",
    )

    plan = migration_service.create_migration_plan(str(root))

    assert "DATABASE_URL" in plan.secrets
    assert "hunter2" not in plan.model_dump_json()


def test_healthcheck_becomes_probes(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))
    api, db = plan.services

    # curl against localhost becomes a real httpGet probe, not an exec probe.
    assert api.readiness_probe is not None
    assert api.readiness_probe.kind == "httpGet"
    assert api.readiness_probe.path == "/health"
    assert api.readiness_probe.port == 8000
    # Liveness restarts the pod, so it must wait out the start period.
    assert api.liveness_probe is not None
    assert api.liveness_probe.initial_delay_seconds == 20

    # CMD-SHELL has no HTTP equivalent and stays an exec probe.
    assert db.readiness_probe is not None
    assert db.readiness_probe.kind == "exec"
    assert db.readiness_probe.command == ["/bin/sh", "-c", "pg_isready -U postgres"]


def test_environment_interpolation_resolved_from_env_file(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))
    api = plan.services[0]
    env = {e.key: e.value for e in api.env}

    # ${DB_PASSWORD} is a secret, so it is placeholdered rather than resolved.
    assert env["DB_PASSWORD"] == REQUIRED_SECRET
    assert env["DB_HOST"] == "db"


def test_unresolved_interpolation_is_reported(sandbox: Path) -> None:
    root = sandbox / "unresolved"
    root.mkdir()
    (root / "docker-compose.yml").write_text(
        "services:\n  api:\n    image: demo:1\n    environment:\n      REGION: ${AWS_REGION}\n",
        encoding="utf-8",
    )

    plan = migration_service.create_migration_plan(str(root))

    assert any(w.code == "UNRESOLVED_INTERPOLATION" for w in plan.warnings)


def test_namespace_override(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project), namespace="staging")

    assert plan.namespace == "staging"
    assert "Namespace/staging" in plan.resources

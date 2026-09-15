"""Tests for manifest generation and validation.

None of these need a cluster: generation writes to tmp_path and validation reads
YAML from disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.errors import InvalidManifest
from src.generators import kubernetes as generator
from src.security import REQUIRED_SECRET
from src.services import migration_service
from src.validators import kubernetes as validator


@pytest.fixture
def manifests(project: Path) -> dict[str, dict]:
    """Generate the demo project's manifests and index them by 'Kind/name'."""
    plan = migration_service.create_migration_plan(str(project))
    result = generator.generate_manifests(plan)
    documents = {}
    for manifest in result.manifests:
        doc = yaml.safe_load(Path(manifest.path).read_text(encoding="utf-8"))
        documents[f"{doc['kind']}/{doc['metadata']['name']}"] = doc
    return documents


@pytest.fixture
def output_dir(project: Path) -> str:
    plan = migration_service.create_migration_plan(str(project))
    return generator.generate_manifests(plan).output_dir


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def test_deployment_generation(manifests: dict[str, dict]) -> None:
    deployment = manifests["Deployment/api"]

    assert deployment["apiVersion"] == "apps/v1"
    assert deployment["spec"]["replicas"] == 2

    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "demo-api:local"
    assert container["ports"][0]["containerPort"] == 8000
    assert container["readinessProbe"]["httpGet"]["path"] == "/health"
    assert container["resources"]["limits"]["memory"] == "512Mi"


def test_deployment_generation_selector_matches_pod_labels(manifests: dict[str, dict]) -> None:
    deployment = manifests["Deployment/api"]
    selector = deployment["spec"]["selector"]["matchLabels"]
    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]

    assert selector  # a controller with no selector adopts nothing
    assert all(pod_labels[k] == v for k, v in selector.items())


def test_statefulset_generation(manifests: dict[str, dict]) -> None:
    statefulset = manifests["StatefulSet/db"]

    assert statefulset["spec"]["serviceName"] == "db"
    claims = statefulset["spec"]["volumeClaimTemplates"]
    assert [c["metadata"]["name"] for c in claims] == ["db-data"]
    assert claims[0]["spec"]["resources"]["requests"]["storage"] == "1Gi"

    mount = statefulset["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]
    assert mount["name"] == "db-data"
    assert mount["mountPath"] == "/var/lib/postgresql/data"


def test_service_generation(manifests: dict[str, dict]) -> None:
    service = manifests["Service/api"]

    assert service["spec"]["type"] == "NodePort"
    port = service["spec"]["ports"][0]
    assert port["port"] == 8000
    assert port["targetPort"] == 8000

    deployment = manifests["Deployment/api"]
    assert service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"]


def test_service_generation_is_headless_for_statefulset(manifests: dict[str, dict]) -> None:
    service = manifests["Service/db"]

    assert service["spec"]["clusterIP"] == "None"
    assert service["spec"]["type"] == "ClusterIP"


def test_configmap_generation(manifests: dict[str, dict]) -> None:
    config = manifests["ConfigMap/api-config"]

    assert config["data"]["LOG_LEVEL"] == "INFO"
    assert config["data"]["DB_HOST"] == "db"
    assert "DB_PASSWORD" not in config["data"]


def test_secret_generation_uses_placeholders(manifests: dict[str, dict]) -> None:
    secret = manifests["Secret/api-secret"]

    assert secret["type"] == "Opaque"
    assert secret["stringData"]["DB_PASSWORD"] == REQUIRED_SECRET
    # The real credential from .env must never be written to disk.
    assert "super-secret" not in yaml.safe_dump(secret)


def test_generation_only_creates_needed_resources(manifests: dict[str, dict]) -> None:
    kinds = {key.split("/")[0] for key in manifests}

    assert kinds == {"Deployment", "StatefulSet", "Service", "ConfigMap", "Secret"}
    # Nothing asked for an Ingress, and the namespace is the default one.
    assert "Ingress" not in kinds
    assert "Namespace" not in kinds


def test_generation_adds_namespace_for_non_default(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project), namespace="staging")
    result = generator.generate_manifests(plan)

    assert any(m.kind == "Namespace" for m in result.manifests)


def test_generation_is_scoped_per_project(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))
    result = generator.generate_manifests(plan)

    # Predictable, project-scoped output so two projects cannot overwrite each other.
    assert Path(result.output_dir).parts[-2:] == ("demo-app", "k8s")


def test_generation_emits_no_yaml_anchors(output_dir: str) -> None:
    """Readiness and liveness share a command list; it must be written out twice."""
    text = (Path(output_dir) / "db-statefulset.yaml").read_text(encoding="utf-8")

    assert "&id" not in text
    assert "*id" not in text


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def test_manifest_validation_accepts_generated_output(output_dir: str) -> None:
    result = validator.validate_manifests(output_dir)

    assert result.valid is True
    assert result.error_count == 0
    # Placeholder secrets are a warning, not an error: they are expected here.
    assert any(i.code == "PLACEHOLDER_SECRET" for i in result.issues)


def test_manifest_validation_reports_missing_path(sandbox: Path) -> None:
    with pytest.raises(InvalidManifest):
        validator.validate_manifests(str(sandbox))


def _write(sandbox: Path, name: str, body: str) -> str:
    directory = sandbox / "manifests"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")
    return str(directory)


def test_selector_validation(sandbox: Path) -> None:
    path = _write(
        sandbox,
        "deploy.yaml",
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: api
          image: api:1
""",
    )

    result = validator.validate_manifests(path)

    assert result.valid is False
    issue = next(i for i in result.issues if i.code == "SELECTOR_MISMATCH")
    assert "does not match" in issue.message
    assert issue.suggestion


def test_port_validation_catches_target_port_mismatch(sandbox: Path) -> None:
    """The example from the spec: containerPort 8000 vs targetPort 8080."""
    path = _write(
        sandbox,
        "app.yaml",
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1
          ports:
            - containerPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector:
    app: api
  ports:
    - port: 80
      targetPort: 8080
""",
    )

    result = validator.validate_manifests(path)

    assert result.valid is False
    issue = next(i for i in result.issues if i.code == "TARGET_PORT_MISMATCH")
    assert "8080" in issue.message and "8000" in issue.message


def test_port_validation_catches_probe_port_mismatch(sandbox: Path) -> None:
    path = _write(
        sandbox,
        "probe.yaml",
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1
          ports:
            - containerPort: 8000
          readinessProbe:
            httpGet:
              path: /health
              port: 9999
""",
    )

    result = validator.validate_manifests(path)

    assert result.valid is False
    assert any(i.code == "PROBE_PORT_MISMATCH" for i in result.issues)


def test_validation_catches_nodeport_out_of_range(sandbox: Path) -> None:
    path = _write(
        sandbox,
        "svc.yaml",
        """
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  type: NodePort
  selector:
    app: api
  ports:
    - port: 80
      nodePort: 8080
""",
    )

    result = validator.validate_manifests(path)

    assert any(i.code == "NODE_PORT_OUT_OF_RANGE" for i in result.issues)


def test_validation_catches_missing_references(sandbox: Path) -> None:
    path = _write(
        sandbox,
        "refs.yaml",
        """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1
          envFrom:
            - configMapRef:
                name: nope-config
            - secretRef:
                name: nope-secret
          volumeMounts:
            - name: data
              mountPath: /data
""",
    )

    result = validator.validate_manifests(path)
    codes = {i.code for i in result.issues}

    assert result.valid is False
    assert "MISSING_CONFIGMAP" in codes
    assert "MISSING_SECRET" in codes
    assert "UNDEFINED_VOLUME" in codes


def test_validation_catches_duplicates_and_bad_api_version(sandbox: Path) -> None:
    path = _write(
        sandbox,
        "dupes.yaml",
        """
apiVersion: v1
kind: ConfigMap
metadata:
  name: shared
data: {}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: shared
data: {}
---
apiVersion: v1
kind: Deployment
metadata:
  name: api
spec:
  selector:
    matchLabels:
      app: api
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1
""",
    )

    result = validator.validate_manifests(path)
    codes = {i.code for i in result.issues}

    assert "DUPLICATE_RESOURCE" in codes
    assert "WRONG_API_VERSION" in codes


def test_validation_catches_invalid_yaml(sandbox: Path) -> None:
    path = _write(sandbox, "broken.yaml", "kind: Service\n  bad: [\n")

    result = validator.validate_manifests(path)

    assert result.valid is False
    assert any(i.code == "INVALID_YAML" for i in result.issues)


def test_validation_catches_missing_name_and_kind(sandbox: Path) -> None:
    path = _write(sandbox, "nameless.yaml", "apiVersion: v1\nkind: Service\nspec:\n  ports: []\n")

    result = validator.validate_manifests(path)
    codes = {i.code for i in result.issues}

    assert "MISSING_NAME" in codes
    assert "SERVICE_NO_PORTS" in codes


def test_validation_catches_statefulset_without_governing_service(sandbox: Path) -> None:
    path = _write(
        sandbox,
        "sts.yaml",
        """
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
spec:
  serviceName: db
  selector:
    matchLabels:
      app: db
  template:
    metadata:
      labels:
        app: db
    spec:
      containers:
        - name: db
          image: postgres:16
""",
    )

    result = validator.validate_manifests(path)

    assert any(i.code == "MISSING_GOVERNING_SERVICE" for i in result.issues)


# --------------------------------------------------------------------------
# Integration: these need a real cluster and are excluded by default.
# Run them with:  pytest -m integration
# --------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_cluster_is_reachable() -> None:
    from src.services import k8s_service

    info = await k8s_service.cluster_info()

    assert info["reachable"] is True
    assert info["node_count"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_server_side_dry_run_accepts_generated_manifests(output_dir: str) -> None:
    """A dry run proves the API server itself accepts the manifests."""
    from src.services import k8s_service

    result = await k8s_service.apply_manifests(output_dir, namespace="default", dry_run=True)

    assert result.dry_run is True
    assert result.applied is False
    assert result.resources

"""Cluster-side logic, against a fake API and a fake kubectl.

These cover what apply/verify/diagnose decide, which is where the real bugs
were; the Kubernetes client and kubectl themselves are not under test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest
import yaml
from kubernetes.client.exceptions import ApiException

from docker2k8s_mcp.errors import DeploymentFailed, InvalidManifest
from docker2k8s_mcp.generators import kubernetes as generator
from docker2k8s_mcp.services import k8s_service, migration_service
from docker2k8s_mcp.validators import kubernetes as validator

NOW = datetime.now(timezone.utc)


def _missing(*_: Any, **__: Any) -> Any:
    raise ApiException(status=404)


def _items(items: list[Any]) -> Any:
    return lambda *_, **__: NS(items=items)


class FakeCluster:
    """Just enough of CoreV1/AppsV1/DiscoveryV1 for the code under test."""

    def __init__(
        self,
        namespaces: set[str] = frozenset(),
        secrets: set[tuple[str, str]] = frozenset(),
        workloads: dict[str, Any] | None = None,
        pods: list[Any] | None = None,
        services: list[Any] | None = None,
        slices: list[Any] | None = None,
    ) -> None:
        self.namespaces = set(namespaces)
        self.secrets = set(secrets)
        self.workloads = workloads or {}
        self.health_calls: list[dict[str, Any]] = []
        self.log_calls: list[dict[str, Any]] = []
        self.clients = k8s_service._Clients(
            core=NS(
                read_namespace=lambda name: NS() if name in self.namespaces else _missing(),
                read_namespaced_secret=lambda name, ns: NS() if (ns, name) in self.secrets else _missing(),
                read_namespaced_service=lambda name, ns: self._workload("Service", name),
                list_namespaced_pod=_items(pods or []),
                list_namespaced_service=_items(services or []),
                list_namespaced_event=_items([]),
                read_namespaced_pod_log=lambda **kw: self.log_calls.append(kw) or "log line",
            ),
            apps=NS(
                read_namespaced_deployment=lambda name, ns: self._workload("Deployment", name),
                read_namespaced_stateful_set=lambda name, ns: self._workload("StatefulSet", name),
                list_namespaced_deployment=_items([]),
                list_namespaced_stateful_set=_items([]),
            ),
            discovery=NS(list_namespaced_endpoint_slice=_items(slices or [])),
            api_client=NS(call_api=self._call_api),
        )

    def _workload(self, kind: str, name: str) -> Any:
        found = self.workloads.get(f"{kind}/{name}")
        return found if found is not None else _missing()

    def _call_api(self, path: str, method: str, **kwargs: Any) -> Any:
        self.health_calls.append({"path": path, **kwargs})
        return NS(read=lambda amount: b'{"status": "healthy"}'[:amount], release_conn=lambda: None)


@pytest.fixture
def kubectl(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record kubectl invocations instead of running them."""
    calls: list[dict[str, Any]] = []

    async def fake(args: list[str], *, stdin: bytes | None = None, timeout: int | None = None):
        docs = list(yaml.safe_load_all(stdin or b""))
        calls.append({"args": args, "docs": docs})
        lines = "\n".join(f"{d['kind'].lower()}/{d['metadata']['name']} created" for d in docs)
        return 0, lines, ""

    monkeypatch.setattr(k8s_service, "_run_kubectl", fake)
    return calls


def use(monkeypatch: pytest.MonkeyPatch, cluster: FakeCluster) -> FakeCluster:
    monkeypatch.setattr(k8s_service, "_load_clients", lambda: cluster.clients)
    return cluster


@pytest.fixture
def staging_bundle(project: Path) -> validator.ManifestBundle:
    plan = migration_service.create_migration_plan(str(project), namespace="staging")
    return validator.load_manifests(generator.generate_manifests(plan).output_dir)


# --------------------------------------------------------------------------
# apply_manifests
# --------------------------------------------------------------------------
async def test_apply_uses_manifest_namespace_and_keeps_existing_secrets(
    monkeypatch: pytest.MonkeyPatch, kubectl: list, staging_bundle: validator.ManifestBundle
) -> None:
    use(monkeypatch, FakeCluster(
        namespaces={"staging"}, secrets={("staging", "api-secret"), ("staging", "db-secret")}
    ))

    result = await k8s_service.apply_manifests(staging_bundle)

    assert result.namespace == "staging"
    assert len(kubectl) == 1 and kubectl[0]["args"] == ["apply", "-f", "-"]  # one process, no --namespace
    applied = kubectl[0]["docs"]
    assert applied[0]["kind"] == "Namespace"
    assert all(d["metadata"].get("namespace") == "staging" for d in applied if d["kind"] != "Namespace")
    # Placeholders must not overwrite the real Secrets already in the cluster.
    assert not any(d["kind"] == "Secret" for d in applied)
    assert sorted(s.split(":")[0] for s in result.skipped) == ["Secret/api-secret", "Secret/db-secret"]


async def test_apply_refuses_a_contradicting_namespace(
    monkeypatch: pytest.MonkeyPatch, kubectl: list, staging_bundle: validator.ManifestBundle
) -> None:
    use(monkeypatch, FakeCluster(namespaces={"staging", "default"}))

    with pytest.raises(InvalidManifest, match="staging"):
        await k8s_service.apply_manifests(staging_bundle, namespace="default")
    assert kubectl == []


async def test_apply_refuses_to_create_placeholder_secrets(
    monkeypatch: pytest.MonkeyPatch, kubectl: list, staging_bundle: validator.ManifestBundle
) -> None:
    use(monkeypatch, FakeCluster(namespaces={"staging"}))

    with pytest.raises(DeploymentFailed, match="placeholders"):
        await k8s_service.apply_manifests(staging_bundle)
    assert kubectl == []


async def test_dry_run_into_a_new_namespace_checks_the_rest_client_side(
    monkeypatch: pytest.MonkeyPatch, kubectl: list, staging_bundle: validator.ManifestBundle
) -> None:
    use(monkeypatch, FakeCluster())

    result = await k8s_service.apply_manifests(staging_bundle, dry_run=True)

    server, client = kubectl
    assert server["args"][-1] == "--dry-run=server"
    assert [d["kind"] for d in server["docs"]] == ["Namespace"]
    assert client["args"][-1] == "--dry-run=client"
    assert {d["kind"] for d in client["docs"]} >= {"Deployment", "StatefulSet", "Secret"}
    assert any("client-side" in w for w in result.warnings)


async def test_apply_refuses_kinds_it_does_not_deploy(
    monkeypatch: pytest.MonkeyPatch, kubectl: list, sandbox: Path
) -> None:
    use(monkeypatch, FakeCluster(namespaces={"default"}))
    path = sandbox / "rbac.yaml"
    path.write_text(
        "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata: {name: admin}\n",
        encoding="utf-8",
    )

    with pytest.raises(InvalidManifest, match="ClusterRoleBinding"):
        await k8s_service.apply_manifests(validator.load_manifests(str(path)))


async def test_apply_refuses_objects_owned_by_another_project(
    monkeypatch: pytest.MonkeyPatch, kubectl: list, staging_bundle: validator.ManifestBundle
) -> None:
    other = NS(metadata=NS(labels={"app.kubernetes.io/part-of": "someone-else"}))
    use(monkeypatch, FakeCluster(
        namespaces={"staging"},
        secrets={("staging", "api-secret"), ("staging", "db-secret")},
        workloads={"Deployment/api": other},
    ))

    with pytest.raises(DeploymentFailed, match="someone-else"):
        await k8s_service.apply_manifests(staging_bundle)
    assert kubectl == []


def test_context_names_with_at_signs_and_arns_are_accepted() -> None:
    for name in ("kubernetes-admin@kubernetes", "arn:aws:eks:us-east-1:1:cluster/prod", "gke_p_z_c"):
        assert k8s_service._validate_context(name) == name
    with pytest.raises(InvalidManifest):
        k8s_service._validate_context("bad\nname")


def test_blank_context_uses_kubeconfig_current_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from kubernetes import client, config

    contexts: list[str | None] = []
    monkeypatch.setattr(k8s_service, "get_settings", lambda: NS(kube_context=""))
    monkeypatch.setattr(
        config, "load_kube_config", lambda *, context=None: contexts.append(context)
    )
    monkeypatch.setattr(client, "ApiClient", lambda: NS())
    monkeypatch.setattr(client, "CoreV1Api", lambda _: NS())
    monkeypatch.setattr(client, "AppsV1Api", lambda _: NS())
    monkeypatch.setattr(client, "DiscoveryV1Api", lambda _: NS())

    k8s_service._load_clients.cache_clear()
    try:
        k8s_service._load_clients()
    finally:
        k8s_service._load_clients.cache_clear()

    assert contexts == [None]


# --------------------------------------------------------------------------
# Status and verification
# --------------------------------------------------------------------------
LABELS = {"app.kubernetes.io/name": "api", "app.kubernetes.io/instance": "demo-api"}


def deployment(generation: int = 1, observed: int = 1, ready: int = 1, updated: int = 1) -> Any:
    return NS(
        metadata=NS(generation=generation),
        spec=NS(replicas=1, selector=NS(match_labels=LABELS)),
        status=NS(
            ready_replicas=ready,
            available_replicas=ready,
            updated_replicas=updated,
            observed_generation=observed,
            conditions=[],
        ),
    )


def pod(name: str, labels: dict[str, str], *, restarts: int = 0, restarted_ago: int | None = None,
        waiting: str | None = None) -> Any:
    last = NS(terminated=NS(finished_at=NOW - timedelta(seconds=restarted_ago), reason="Error")) if restarted_ago else None
    state = NS(running=None, waiting=NS(reason=waiting, message=None), terminated=None) if waiting else NS(running=True)
    ready = waiting is None
    return NS(
        metadata=NS(name=name, labels=labels, deletion_timestamp=None),
        spec=NS(node_name="node-1"),
        status=NS(
            phase="Running",
            pod_ip="10.0.0.2",
            start_time=NOW - timedelta(hours=1),
            conditions=[NS(type="Ready", status="True" if ready else "False", reason=None)],
            container_statuses=[
                NS(name="api", ready=ready, restart_count=restarts, state=state, last_state=last)
            ],
        ),
    )


def service(name: str, *, port_name: str = "http", headless: bool = False, kind: str = "NodePort") -> Any:
    return NS(
        metadata=NS(name=name),
        spec=NS(
            ports=[NS(name=port_name, port=8000, target_port=8000, protocol="TCP", node_port=None)],
            type="ClusterIP" if headless else kind,
            cluster_ip="None" if headless else "10.96.0.10",
            selector=LABELS,
        ),
    )


READY_SLICE = NS(
    metadata=NS(labels={"kubernetes.io/service-name": "api"}),
    endpoints=[NS(conditions=NS(ready=True), target_ref=NS(uid="pod-1"), addresses=["10.0.0.2"])],
)


@pytest.mark.parametrize(
    ("generation", "observed", "updated", "healthy"),
    [(2, 1, 1, False), (1, 1, 0, False), (1, 1, 1, True)],
)
async def test_status_requires_a_completed_rollout(
    monkeypatch: pytest.MonkeyPatch, generation: int, observed: int, updated: int, healthy: bool
) -> None:
    use(monkeypatch, FakeCluster(
        workloads={"Deployment/api": deployment(generation=generation, observed=observed, updated=updated)}
    ))

    assert (await k8s_service.get_deployment_status("api", "default")).healthy is healthy


async def test_verify_matches_pods_by_selector_and_tolerates_settled_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_labels = {"app.kubernetes.io/name": "api-worker", "app.kubernetes.io/instance": "demo-api-worker"}
    use(monkeypatch, FakeCluster(
        workloads={"Deployment/api": deployment()},
        pods=[
            pod("api-5d9f-abc", LABELS, restarts=2, restarted_ago=1200),
            # Shares the 'api-' prefix but belongs to another workload.
            pod("api-worker-7c8-xyz", worker_labels, restarts=9, waiting="CrashLoopBackOff"),
        ],
        services=[service("api")],
        slices=[READY_SLICE],
    ))

    result = await k8s_service.verify_deployment("default", ["api"])

    assert result.healthy, [c for c in result.checks if not c.passed]
    assert [p.name for p in result.pods] == ["api-5d9f-abc"]
    restarts = next(c for c in result.checks if c.name == "pod restarts")
    assert "now stable" in restarts.detail


async def test_verify_fails_on_a_recent_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, FakeCluster(
        workloads={"Deployment/api": deployment()},
        pods=[pod("api-5d9f-abc", LABELS, restarts=1, restarted_ago=30)],
        services=[service("api")],
        slices=[READY_SLICE],
    ))

    result = await k8s_service.verify_deployment("default", ["api"])

    assert not result.healthy
    assert not next(c for c in result.checks if c.name == "pod restarts").passed


async def test_health_check_probes_http_services_and_reads_little(monkeypatch: pytest.MonkeyPatch) -> None:
    db = deployment()
    cluster = use(monkeypatch, FakeCluster(
        workloads={"Deployment/api": deployment(), "StatefulSet/db": db},
        pods=[pod("api-5d9f-abc", LABELS)],
        services=[service("api"), service("db", port_name="postgres", headless=True)],
        slices=[READY_SLICE],
    ))

    result = await k8s_service.verify_deployment("default", ["api", "db"], health_path="health")

    # The database is not sent an HTTP request.
    assert [c["path"] for c in cluster.health_calls] == [
        "/api/v1/namespaces/default/services/api:8000/proxy/health"
    ]
    call = cluster.health_calls[0]
    assert call["_preload_content"] is False and call["_request_timeout"]
    assert next(c for c in result.checks if c.name.startswith("health")).passed


@pytest.mark.parametrize("path", ["/../../../api/v1/secrets", "/%2e%2e/secrets", "/a b", "/x#y"])
async def test_health_path_cannot_leave_the_service_proxy(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    use(monkeypatch, FakeCluster())

    with pytest.raises(InvalidManifest):
        await k8s_service.verify_deployment("default", ["api"], health_path=path)


async def test_diagnose_reports_an_empty_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, FakeCluster())

    diagnosis = await k8s_service.diagnose_deployment("default")

    assert diagnosis.healthy is False
    assert diagnosis.findings[0].problem == "Nothing deployed"


async def test_pod_logs_are_size_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = use(monkeypatch, FakeCluster())

    await k8s_service.get_pod_logs("api-5d9f-abc", "default")

    assert cluster.log_calls[0]["limit_bytes"] == 256 * 1024

"""Render a MigrationPlan into Kubernetes YAML.

Only the resources a plan actually needs are produced: a stateless API gets a
Deployment, a Service and a ConfigMap, while a database additionally gets a
StatefulSet with volumeClaimTemplates and a headless Service.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from ..config import get_settings
from ..schemas import (
    GeneratedManifest,
    GenerationResult,
    MigrationPlan,
    ProbePlan,
    ServicePlan,
)
from ..security import REQUIRED_SECRET

logger = logging.getLogger(__name__)

MANAGED_BY = "docker2k8s-mcp"


def generate_manifests(
    plan: MigrationPlan,
    output_dir: str | Path | None = None,
    include_ingress: bool = False,
    ingress_host: str | None = None,
) -> GenerationResult:
    """Write one YAML file per Kubernetes resource and return an index of them."""
    settings = get_settings()
    target = Path(output_dir) if output_dir else settings.generated_dir / plan.project_name / "k8s"
    target.mkdir(parents=True, exist_ok=True)

    manifests: list[GeneratedManifest] = []
    warnings: list[str] = []

    if plan.namespace != "default":
        manifests.append(_write(target, "namespace.yaml", _namespace(plan)))

    for service in plan.services:
        if service.config_map_name:
            manifests.append(
                _write(target, f"{service.name}-configmap.yaml", _config_map(plan, service))
            )
        if service.secret_name:
            manifests.append(_write(target, f"{service.name}-secret.yaml", _secret(plan, service)))
            warnings.append(
                f"Secret '{service.secret_name}' contains {REQUIRED_SECRET} placeholders; "
                "replace them before deploying."
            )

        # A StatefulSet carries its own storage via volumeClaimTemplates; a
        # Deployment needs standalone PVCs.
        if service.workload_kind == "Deployment":
            for volume in service.volumes:
                if volume.source_kind == "named":
                    manifests.append(
                        _write(
                            target,
                            f"{service.name}-{volume.name}-pvc.yaml",
                            _pvc(plan, service, volume.name, volume.size, volume.access_mode, volume.storage_class),
                        )
                    )

        filename = f"{service.name}-{service.workload_kind.lower()}.yaml"
        manifests.append(_write(target, filename, _workload(plan, service)))

        if service.create_service:
            manifests.append(_write(target, f"{service.name}-service.yaml", _service(plan, service)))

        if include_ingress and service.service_type in {"NodePort", "LoadBalancer"}:
            manifests.append(
                _write(target, f"{service.name}-ingress.yaml", _ingress(plan, service, ingress_host))
            )

    logger.info("Generated %d Kubernetes resource(s) in %s", len(manifests), target)
    return GenerationResult(
        output_dir=str(target),
        project_name=plan.project_name,
        namespace=plan.namespace,
        manifests=manifests,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Resource builders
# --------------------------------------------------------------------------
def _labels(plan: MigrationPlan, service: ServicePlan | None = None) -> dict[str, str]:
    labels = {
        "app.kubernetes.io/part-of": plan.project_name,
        "app.kubernetes.io/managed-by": MANAGED_BY,
    }
    if service is not None:
        labels["app.kubernetes.io/name"] = service.name
        labels["app.kubernetes.io/instance"] = f"{plan.project_name}-{service.name}"
    return labels


def _selector(plan: MigrationPlan, service: ServicePlan) -> dict[str, str]:
    """The selector is a stable subset of the labels: it is immutable once applied."""
    return {
        "app.kubernetes.io/name": service.name,
        "app.kubernetes.io/instance": f"{plan.project_name}-{service.name}",
    }


def _meta(plan: MigrationPlan, name: str, service: ServicePlan | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": plan.namespace,
        "labels": _labels(plan, service),
    }


def _namespace(plan: MigrationPlan) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": plan.namespace, "labels": _labels(plan)},
    }


def _config_map(plan: MigrationPlan, service: ServicePlan) -> dict[str, Any]:
    data = {e.key: str(e.value if e.value is not None else "") for e in service.env if not e.is_secret}
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(plan, service.config_map_name or f"{service.name}-config", service),
        "data": data,
    }


def _secret(plan: MigrationPlan, service: ServicePlan) -> dict[str, Any]:
    # stringData keeps the file readable and avoids base64-encoding placeholders
    # that would otherwise look like real (but broken) credentials.
    data = {e.key: REQUIRED_SECRET for e in service.env if e.is_secret}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _meta(plan, service.secret_name or f"{service.name}-secret", service),
        "type": "Opaque",
        "stringData": data,
    }


def _pvc(
    plan: MigrationPlan,
    service: ServicePlan,
    volume_name: str,
    size: str,
    access_mode: str,
    storage_class: str | None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "accessModes": [access_mode],
        "resources": {"requests": {"storage": size}},
    }
    if storage_class:
        spec["storageClassName"] = storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": _meta(plan, f"{service.name}-{volume_name}", service),
        "spec": spec,
    }


def _probe(probe: ProbePlan) -> dict[str, Any]:
    body: dict[str, Any] = {
        "initialDelaySeconds": probe.initial_delay_seconds,
        "periodSeconds": probe.period_seconds,
        "timeoutSeconds": probe.timeout_seconds,
        "failureThreshold": probe.failure_threshold,
    }
    if probe.kind == "httpGet":
        body["httpGet"] = {"path": probe.path or "/", "port": probe.port}
    elif probe.kind == "tcpSocket":
        body["tcpSocket"] = {"port": probe.port}
    else:
        body["exec"] = {"command": probe.command or []}
    return body


def _container(plan: MigrationPlan, service: ServicePlan) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": service.name,
        "image": service.image,
        "imagePullPolicy": service.image_pull_policy,
    }
    if service.command:
        container["command"] = service.command
    if service.args:
        container["args"] = service.args
    if service.container_ports:
        container["ports"] = [
            {"name": p.name, "containerPort": p.container_port, "protocol": p.protocol}
            for p in service.container_ports
        ]

    env_from: list[dict[str, Any]] = []
    if service.config_map_name:
        env_from.append({"configMapRef": {"name": service.config_map_name}})
    if service.secret_name:
        env_from.append({"secretRef": {"name": service.secret_name}})
    if env_from:
        container["envFrom"] = env_from

    if service.volumes:
        container["volumeMounts"] = [
            {"name": v.name, "mountPath": v.mount_path, "readOnly": v.read_only}
            if v.read_only
            else {"name": v.name, "mountPath": v.mount_path}
            for v in service.volumes
        ]

    if service.readiness_probe:
        container["readinessProbe"] = _probe(service.readiness_probe)
    if service.liveness_probe:
        container["livenessProbe"] = _probe(service.liveness_probe)

    resources = _resources(service)
    if resources:
        container["resources"] = resources
    return container


def _resources(service: ServicePlan) -> dict[str, Any]:
    limits: dict[str, str] = {}
    requests: dict[str, str] = {}
    if service.resources:
        if service.resources.cpu_limit:
            limits["cpu"] = service.resources.cpu_limit
        if service.resources.memory_limit:
            limits["memory"] = service.resources.memory_limit
        if service.resources.cpu_request:
            requests["cpu"] = service.resources.cpu_request
        if service.resources.memory_request:
            requests["memory"] = service.resources.memory_request
    body: dict[str, Any] = {}
    if limits:
        body["limits"] = limits
    if requests:
        body["requests"] = requests
    return body


def _workload(plan: MigrationPlan, service: ServicePlan) -> dict[str, Any]:
    pod_spec: dict[str, Any] = {"containers": [_container(plan, service)]}

    # StatefulSet volumes come from volumeClaimTemplates, so only Deployments
    # (and emptyDir volumes) need an explicit pod-level volumes list.
    pod_volumes: list[dict[str, Any]] = []
    for volume in service.volumes:
        if volume.source_kind == "anonymous":
            pod_volumes.append({"name": volume.name, "emptyDir": {}})
        elif service.workload_kind == "Deployment":
            pod_volumes.append(
                {
                    "name": volume.name,
                    "persistentVolumeClaim": {"claimName": f"{service.name}-{volume.name}"},
                }
            )
    if pod_volumes:
        pod_spec["volumes"] = pod_volumes

    template = {
        "metadata": {"labels": _labels(plan, service)},
        "spec": pod_spec,
    }

    spec: dict[str, Any] = {
        "replicas": service.replicas,
        "selector": {"matchLabels": _selector(plan, service)},
        "template": template,
    }

    if service.workload_kind == "StatefulSet":
        spec["serviceName"] = service.name
        claims = [
            {
                "metadata": {"name": volume.name},
                "spec": {
                    "accessModes": [volume.access_mode],
                    "resources": {"requests": {"storage": volume.size}},
                    **({"storageClassName": volume.storage_class} if volume.storage_class else {}),
                },
            }
            for volume in service.volumes
            if volume.source_kind == "named"
        ]
        if claims:
            spec["volumeClaimTemplates"] = claims
        api_version = "apps/v1"
    else:
        spec["strategy"] = {"type": "RollingUpdate"}
        api_version = "apps/v1"

    return {
        "apiVersion": api_version,
        "kind": service.workload_kind,
        "metadata": _meta(plan, service.name, service),
        "spec": spec,
    }


def _service(plan: MigrationPlan, service: ServicePlan) -> dict[str, Any]:
    ports: list[dict[str, Any]] = []
    for port in service.service_ports:
        entry: dict[str, Any] = {
            "name": port.name,
            "port": port.port,
            "targetPort": port.target_port,
            "protocol": port.protocol,
        }
        if port.node_port and service.service_type == "NodePort":
            entry["nodePort"] = port.node_port
        ports.append(entry)

    spec: dict[str, Any] = {
        "selector": _selector(plan, service),
        "ports": ports,
        "type": service.service_type,
    }
    if service.headless:
        # Headless gives each StatefulSet pod a stable DNS name.
        spec["clusterIP"] = "None"
        spec["type"] = "ClusterIP"

    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta(plan, service.name, service),
        "spec": spec,
    }


def _ingress(plan: MigrationPlan, service: ServicePlan, host: str | None) -> dict[str, Any]:
    port = service.service_ports[0].port if service.service_ports else 80
    rule: dict[str, Any] = {
        "http": {
            "paths": [
                {
                    "path": "/",
                    "pathType": "Prefix",
                    "backend": {"service": {"name": service.name, "port": {"number": port}}},
                }
            ]
        }
    }
    if host:
        rule["host"] = host
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": _meta(plan, service.name, service),
        "spec": {"rules": [rule]},
    }


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
class _NoAliasDumper(yaml.SafeDumper):
    """Emit repeated objects in full.

    Readiness and liveness probes legitimately share a command list, and PyYAML
    would otherwise write it once as an anchor (&id001) and reference it. That is
    valid YAML but confusing to read and unfriendly to some manifest tooling.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _write(directory: Path, filename: str, document: dict[str, Any]) -> GeneratedManifest:
    path = directory / filename
    header = (
        f"# Generated by {MANAGED_BY}. Review before applying.\n"
        f"# Source: Docker Compose migration plan.\n"
    )
    body = yaml.dump(
        document, Dumper=_NoAliasDumper, sort_keys=False, default_flow_style=False, width=100
    )
    path.write_text(header + body, encoding="utf-8")
    return GeneratedManifest(
        path=str(path),
        kind=document["kind"],
        name=document["metadata"]["name"],
    )

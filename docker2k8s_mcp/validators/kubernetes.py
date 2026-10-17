"""Validate generated Kubernetes manifests before anything touches a cluster.

The point is to catch the mistakes that produce a green `kubectl apply` and a
broken application: a Service whose selector matches nothing, a targetPort that
no container listens on, a reference to a ConfigMap that was never generated.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..errors import InvalidManifest
from ..schemas import ValidationIssue, ValidationResult
from ..security import REQUIRED_SECRET, resolve_project_path

logger = logging.getLogger(__name__)

#: kind -> the apiVersion(s) we expect. Used to catch copy-paste mistakes.
EXPECTED_API_VERSIONS: dict[str, set[str]] = {
    "Pod": {"v1"},
    "Service": {"v1"},
    "ConfigMap": {"v1"},
    "Secret": {"v1"},
    "Namespace": {"v1"},
    "PersistentVolumeClaim": {"v1"},
    "ServiceAccount": {"v1"},
    "Deployment": {"apps/v1"},
    "StatefulSet": {"apps/v1"},
    "DaemonSet": {"apps/v1"},
    "ReplicaSet": {"apps/v1"},
    "Job": {"batch/v1"},
    "CronJob": {"batch/v1"},
    "Ingress": {"networking.k8s.io/v1"},
    "NetworkPolicy": {"networking.k8s.io/v1"},
    "HorizontalPodAutoscaler": {"autoscaling/v2", "autoscaling/v1"},
}

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob"}

_DNS1123_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
_DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS1035_LABEL = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
_LABEL_VALUE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")
_PORT_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_PLACEHOLDER_B64 = base64.b64encode(REQUIRED_SECRET.encode()).decode()

#: Kinds whose names are DNS labels rather than subdomains, and which rule applies.
_LABEL_NAMED_KINDS = {"Service": _DNS1035_LABEL, "Namespace": _DNS1123_LABEL}


class _ManifestLoader(yaml.SafeLoader):
    """SafeLoader that keeps timestamps as the strings they were written as.

    Documents are re-serialised for 'kubectl apply -f -'; a datetime would come
    back out in a different format than the author wrote.
    """


_ManifestLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class ManifestDoc:
    """A parsed manifest document plus where it came from."""

    def __init__(self, doc: dict[str, Any], file: Path, index: int) -> None:
        self.doc = doc
        self.file = file
        self.index = index

    @property
    def kind(self) -> str:
        return str(self.doc.get("kind", ""))

    @property
    def name(self) -> str:
        return str((self.doc.get("metadata") or {}).get("name", ""))

    @property
    def namespace(self) -> str:
        return str((self.doc.get("metadata") or {}).get("namespace", "default"))

    @property
    def declared_namespace(self) -> str | None:
        return (self.doc.get("metadata") or {}).get("namespace")

    @property
    def ref(self) -> str:
        return f"{self.kind}/{self.name}" if self.name else f"{self.kind or 'unknown'}[{self.index}]"


_Resource = ManifestDoc


@dataclass
class ManifestBundle:
    """Every document under a path, parsed once and shared by validate and apply."""

    target: Path
    files: list[Path]
    resources: list[ManifestDoc] = field(default_factory=list)
    parse_issues: list[ValidationIssue] = field(default_factory=list)


def load_manifests(path: str) -> ManifestBundle:
    """Parse a manifest file or every YAML file under a directory."""
    target = resolve_project_path(path)
    files = _collect_files(target)
    if not files:
        raise InvalidManifest(
            f"No YAML manifests found at '{target}'.",
            hint="Run generate_manifests first, then validate its output_dir.",
        )
    bundle = ManifestBundle(target=target, files=files)
    for file in files:
        bundle.resources.extend(_parse_file(file, bundle.parse_issues))
    return bundle


def validate_manifests(path: str) -> ValidationResult:
    """Validate a manifest file or a directory of them."""
    return validate_bundle(load_manifests(path))


def validate_bundle(bundle: ManifestBundle) -> ValidationResult:
    """Run every check over documents that were already loaded."""
    files, resources, target = bundle.files, bundle.resources, bundle.target
    logger.info("Validating %d manifest file(s) in %s", len(files), target)
    issues: list[ValidationIssue] = list(bundle.parse_issues)

    if resources:
        _check_duplicates(resources, issues)
        _check_common(resources, issues)
        _check_workloads(resources, issues)
        _check_services(resources, issues)
        _check_references(resources, issues)
        _check_secrets(resources, issues)

    errors = sum(1 for i in issues if i.severity == "error")
    warnings = sum(1 for i in issues if i.severity == "warning")
    valid = errors == 0

    summary = (
        f"{len(resources)} resource(s) in {len(files)} file(s): "
        f"{errors} error(s), {warnings} warning(s). "
        + ("Safe to apply." if valid else "Fix the errors before applying.")
    )
    logger.info("Kubernetes validation %s: %s", "passed" if valid else "failed", summary)

    return ValidationResult(
        valid=valid,
        path=str(target),
        files_checked=len(files),
        resources=[r.ref for r in resources],
        error_count=errors,
        warning_count=warnings,
        issues=issues,
        summary=summary,
    )


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _collect_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(p for p in target.rglob("*") if p.suffix in {".yaml", ".yml"} and p.is_file())


def _parse_file(file: Path, issues: list[ValidationIssue]) -> list[_Resource]:
    try:
        with file.open(encoding="utf-8", errors="replace") as stream:
            documents = list(yaml.load_all(stream, Loader=_ManifestLoader))  # noqa: S506
    except yaml.YAMLError as exc:
        issues.append(
            ValidationIssue(
                severity="error",
                code="INVALID_YAML",
                message=f"{file.name} is not valid YAML: {exc}",
                file=str(file),
                suggestion="Fix the YAML syntax; nothing in this file could be checked.",
            )
        )
        return []

    resources: list[_Resource] = []
    for index, doc in enumerate(documents):
        if doc is None:
            continue
        if not isinstance(doc, dict):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="NOT_A_MAPPING",
                    message=f"{file.name} document {index} is not a mapping.",
                    file=str(file),
                    suggestion="Every Kubernetes manifest must be a YAML object.",
                )
            )
            continue
        resources.append(_Resource(doc, file, index))
    return resources


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def _check_common(resources: Iterable[_Resource], issues: list[ValidationIssue]) -> None:
    """apiVersion / kind / metadata.name basics."""
    for resource in resources:
        doc = resource.doc
        if not doc.get("kind"):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="MISSING_KIND",
                    message=f"{resource.file.name} document {resource.index} has no 'kind'.",
                    file=str(resource.file),
                    suggestion="Add a kind, e.g. 'kind: Deployment'.",
                )
            )
            continue
        if not doc.get("apiVersion"):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="MISSING_API_VERSION",
                    message=f"{resource.ref} has no 'apiVersion'.",
                    file=str(resource.file),
                    resource=resource.ref,
                    suggestion=f"Expected one of: {', '.join(EXPECTED_API_VERSIONS.get(resource.kind, {'v1'}))}.",
                )
            )
        else:
            expected = EXPECTED_API_VERSIONS.get(resource.kind)
            if expected and doc["apiVersion"] not in expected:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="WRONG_API_VERSION",
                        message=(
                            f"{resource.ref} uses apiVersion '{doc['apiVersion']}' but "
                            f"{resource.kind} requires {' or '.join(sorted(expected))}."
                        ),
                        file=str(resource.file),
                        resource=resource.ref,
                        suggestion=f"Change apiVersion to '{sorted(expected)[0]}'.",
                    )
                )

        if not resource.name:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="MISSING_NAME",
                    message=f"{resource.kind} in {resource.file.name} has no metadata.name.",
                    file=str(resource.file),
                    suggestion="Every resource needs a unique metadata.name.",
                )
            )
        elif not _valid_name(resource.kind, resource.name):
            rule = (
                "at most 63 characters, lowercase alphanumeric or '-', starting with a letter"
                if resource.kind == "Service"
                else "lowercase alphanumeric, '-' or '.', starting and ending alphanumeric"
            )
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="INVALID_NAME",
                    message=f"'{resource.name}' is not a valid {resource.kind} name: {rule}.",
                    file=str(resource.file),
                    resource=resource.ref,
                    suggestion="Rename to something like 'my-service'.",
                )
            )

        _check_labels(resource, issues)


def _check_labels(resource: _Resource, issues: list[ValidationIssue]) -> None:
    """Label values are limited to 63 characters of a restricted alphabet."""
    doc = resource.doc
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    template = _pod_template(resource) if resource.kind in WORKLOAD_KINDS else None
    if resource.kind == "Service":
        selector = spec.get("selector")
    else:
        selector = (spec.get("selector") or {}).get("matchLabels") if isinstance(spec.get("selector"), dict) else None
    sources = [
        (doc.get("metadata") or {}).get("labels"),
        ((template or {}).get("metadata") or {}).get("labels"),
        selector,
    ]
    bad = sorted(
        {
            f"{key}={value}"
            for labels in sources
            if isinstance(labels, dict)
            for key, value in labels.items()
            if len(str(value)) > 63 or not _LABEL_VALUE.match(str(value))
        }
    )
    if bad:
        issues.append(
            ValidationIssue(
                severity="error",
                code="INVALID_LABEL_VALUE",
                message=f"{resource.ref} has invalid label value(s): {', '.join(bad)}.",
                file=str(resource.file),
                resource=resource.ref,
                suggestion=(
                    "Label values must be at most 63 characters of alphanumerics, '-', '_' "
                    "or '.', starting and ending with an alphanumeric."
                ),
            )
        )


def _check_duplicates(resources: list[_Resource], issues: list[ValidationIssue]) -> None:
    seen: dict[tuple[str, str, str], _Resource] = {}
    for resource in resources:
        if not resource.kind or not resource.name:
            continue
        key = (resource.kind, resource.namespace, resource.name)
        if key in seen:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="DUPLICATE_RESOURCE",
                    message=(
                        f"{resource.ref} is defined twice: in {seen[key].file.name} and "
                        f"{resource.file.name}."
                    ),
                    file=str(resource.file),
                    resource=resource.ref,
                    suggestion="Remove one definition; the later one silently wins on apply.",
                )
            )
        else:
            seen[key] = resource


def _pod_template(resource: _Resource) -> dict[str, Any] | None:
    spec = resource.doc.get("spec") or {}
    if resource.kind == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    template = spec.get("template")
    return template if isinstance(template, dict) else None


def _containers(resource: _Resource) -> list[dict[str, Any]]:
    template = _pod_template(resource)
    if template is None:
        return []
    pod_spec = template.get("spec") or {}
    containers = pod_spec.get("containers") or []
    return [c for c in containers if isinstance(c, dict)]


def _check_workloads(resources: list[_Resource], issues: list[ValidationIssue]) -> None:
    service_names = {r.name for r in resources if r.kind == "Service"}

    for resource in resources:
        if resource.kind not in WORKLOAD_KINDS:
            continue
        spec = resource.doc.get("spec") or {}
        template = _pod_template(resource)

        if template is None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="MISSING_POD_TEMPLATE",
                    message=f"{resource.ref} has no spec.template.",
                    file=str(resource.file),
                    resource=resource.ref,
                    suggestion="A workload must define the pod it runs.",
                )
            )
            continue

        pod_labels = ((template.get("metadata") or {}).get("labels")) or {}
        containers = _containers(resource)

        if not containers:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="NO_CONTAINERS",
                    message=f"{resource.ref} declares no containers.",
                    file=str(resource.file),
                    resource=resource.ref,
                    suggestion="Add at least one container with a name and image.",
                )
            )

        for container in containers:
            if not container.get("name"):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="CONTAINER_NO_NAME",
                        message=f"{resource.ref} has a container without a name.",
                        file=str(resource.file),
                        resource=resource.ref,
                    )
                )
            image = container.get("image")
            if not image:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="CONTAINER_NO_IMAGE",
                        message=f"{resource.ref} container '{container.get('name')}' has no image.",
                        file=str(resource.file),
                        resource=resource.ref,
                    )
                )
            elif ":" not in str(image).split("/")[-1]:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="UNTAGGED_IMAGE",
                        message=f"{resource.ref} image '{image}' has no tag; ':latest' is implied.",
                        file=str(resource.file),
                        resource=resource.ref,
                        suggestion="Pin an explicit tag or digest for reproducible rollouts.",
                    )
                )

            _check_port_names(
                resource,
                [p.get("name") for p in container.get("ports") or [] if isinstance(p, dict)],
                f"container '{container.get('name')}'",
                issues,
                max_length=15,
            )
            for port in container.get("ports") or []:
                if not isinstance(port, dict):
                    continue
                number = port.get("containerPort")
                if not isinstance(number, int) or not 1 <= number <= 65535:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="INVALID_CONTAINER_PORT",
                            message=f"{resource.ref} has an invalid containerPort '{number}'.",
                            file=str(resource.file),
                            resource=resource.ref,
                            suggestion="containerPort must be an integer between 1 and 65535.",
                        )
                    )
            _check_probes(resource, container, issues)

        # --- selector ------------------------------------------------------
        if resource.kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"}:
            selector = (spec.get("selector") or {}).get("matchLabels")
            if not selector:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="MISSING_SELECTOR",
                        message=f"{resource.ref} has no spec.selector.matchLabels.",
                        file=str(resource.file),
                        resource=resource.ref,
                        suggestion="The selector tells the controller which pods it owns.",
                    )
                )
            else:
                missing = {k: v for k, v in selector.items() if pod_labels.get(k) != v}
                if missing:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="SELECTOR_MISMATCH",
                            message=(
                                f"{resource.ref} selector {selector} does not match its pod "
                                f"template labels {pod_labels}."
                            ),
                            file=str(resource.file),
                            resource=resource.ref,
                            suggestion=(
                                "The controller would create pods it does not own and scale "
                                f"forever. Add {missing} to spec.template.metadata.labels."
                            ),
                        )
                    )

        if resource.kind == "StatefulSet":
            service_name = spec.get("serviceName")
            if not service_name:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="MISSING_SERVICE_NAME",
                        message=f"{resource.ref} has no spec.serviceName.",
                        file=str(resource.file),
                        resource=resource.ref,
                        suggestion="A StatefulSet needs a governing headless Service.",
                    )
                )
            elif service_name not in service_names:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="MISSING_GOVERNING_SERVICE",
                        message=(
                            f"{resource.ref} references serviceName '{service_name}' but no "
                            "such Service is defined."
                        ),
                        file=str(resource.file),
                        resource=resource.ref,
                        suggestion=f"Add a headless Service named '{service_name}' (clusterIP: None).",
                    )
                )

        replicas = spec.get("replicas")
        if replicas is not None and (not isinstance(replicas, int) or replicas < 0):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="INVALID_REPLICAS",
                    message=f"{resource.ref} has invalid replicas '{replicas}'.",
                    file=str(resource.file),
                    resource=resource.ref,
                )
            )

        _check_volume_mounts(resource, issues)


def _check_probes(resource: _Resource, container: dict[str, Any], issues: list[ValidationIssue]) -> None:
    """A probe pointing at a port the container does not declare never succeeds."""
    declared = {p.get("containerPort") for p in container.get("ports") or [] if isinstance(p, dict)}
    named = {p.get("name") for p in container.get("ports") or [] if isinstance(p, dict)}

    for probe_key in ("readinessProbe", "livenessProbe", "startupProbe"):
        probe = container.get(probe_key)
        if not isinstance(probe, dict):
            continue
        handlers = [k for k in ("httpGet", "tcpSocket", "exec", "grpc") if k in probe]
        if not handlers:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="PROBE_NO_HANDLER",
                    message=f"{resource.ref} {probe_key} defines no httpGet/tcpSocket/exec handler.",
                    file=str(resource.file),
                    resource=resource.ref,
                )
            )
            continue
        for handler in ("httpGet", "tcpSocket"):
            if handler not in probe:
                continue
            port = probe[handler].get("port")
            if isinstance(port, int) and declared and port not in declared:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="PROBE_PORT_MISMATCH",
                        message=(
                            f"{resource.ref} {probe_key} targets port {port}, but the container "
                            f"declares {sorted(p for p in declared if p is not None)}."
                        ),
                        file=str(resource.file),
                        resource=resource.ref,
                        suggestion=(
                            "The probe will always fail and the pod will never become ready. "
                            "Point the probe at the port the application listens on."
                        ),
                    )
                )
            elif isinstance(port, str) and named and port not in named:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="PROBE_PORT_NAME_UNKNOWN",
                        message=f"{resource.ref} {probe_key} targets port name '{port}', which is not declared.",
                        file=str(resource.file),
                        resource=resource.ref,
                    )
                )


def _check_volume_mounts(resource: _Resource, issues: list[ValidationIssue]) -> None:
    """Every volumeMount must resolve to a pod volume or a volumeClaimTemplate."""
    template = _pod_template(resource)
    if template is None:
        return
    pod_spec = template.get("spec") or {}
    volume_names = {v.get("name") for v in pod_spec.get("volumes") or [] if isinstance(v, dict)}
    claim_names = {
        (c.get("metadata") or {}).get("name")
        for c in (resource.doc.get("spec") or {}).get("volumeClaimTemplates") or []
        if isinstance(c, dict)
    }
    available = volume_names | claim_names

    for container in _containers(resource):
        for mount in container.get("volumeMounts") or []:
            if not isinstance(mount, dict):
                continue
            name = mount.get("name")
            if not mount.get("mountPath"):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="MOUNT_NO_PATH",
                        message=f"{resource.ref} volumeMount '{name}' has no mountPath.",
                        file=str(resource.file),
                        resource=resource.ref,
                    )
                )
            if name not in available:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="UNDEFINED_VOLUME",
                        message=(
                            f"{resource.ref} mounts volume '{name}', which is not defined in "
                            "spec.template.spec.volumes or spec.volumeClaimTemplates."
                        ),
                        file=str(resource.file),
                        resource=resource.ref,
                        suggestion=(
                            f"Add a volume named '{name}' (emptyDir, configMap, secret or "
                            "persistentVolumeClaim)."
                        ),
                    )
                )


def _check_services(resources: list[_Resource], issues: list[ValidationIssue]) -> None:
    """Services must select real pods and target real container ports."""
    workloads = [r for r in resources if r.kind in WORKLOAD_KINDS]

    for resource in resources:
        if resource.kind != "Service":
            continue
        spec = resource.doc.get("spec") or {}
        selector = spec.get("selector")
        ports = spec.get("ports") or []
        service_type = spec.get("type", "ClusterIP")

        if not ports:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="SERVICE_NO_PORTS",
                    message=f"{resource.ref} defines no ports.",
                    file=str(resource.file),
                    resource=resource.ref,
                )
            )

        port_dicts = [p for p in ports if isinstance(p, dict)]
        if len(port_dicts) > 1 and any(not p.get("name") for p in port_dicts):
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="MULTIPORT_UNNAMED",
                    message=f"{resource.ref} has multiple ports; every port must be named.",
                    file=str(resource.file),
                    resource=resource.ref,
                )
            )
        _check_port_names(resource, [p.get("name") for p in port_dicts], "spec.ports", issues, 63)
        seen: set[tuple[Any, str]] = set()
        for port in port_dicts:
            key = (port.get("port"), str(port.get("protocol") or "TCP"))
            if key in seen:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="DUPLICATE_SERVICE_PORT",
                        message=f"{resource.ref} exposes port {key[0]}/{key[1]} more than once.",
                        file=str(resource.file),
                        resource=resource.ref,
                    )
                )
            seen.add(key)

        if not selector:
            if spec.get("clusterIP") != "None":
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="SERVICE_NO_SELECTOR",
                        message=f"{resource.ref} has no selector and will never get endpoints.",
                        file=str(resource.file),
                        resource=resource.ref,
                        suggestion="Add a selector, or manage Endpoints manually.",
                    )
                )
            continue

        matched = [w for w in workloads if _labels_match(selector, w)]
        if not matched:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="SERVICE_SELECTOR_MATCHES_NOTHING",
                    message=(
                        f"{resource.ref} selector {selector} matches no pod template in these "
                        "manifests."
                    ),
                    file=str(resource.file),
                    resource=resource.ref,
                    suggestion=(
                        "The Service will have zero endpoints and every request will be "
                        "refused. Align the selector with the workload's pod labels."
                    ),
                )
            )
            # Deliberately no early exit: the port checks below still apply, and
            # reporting one problem per resource would hide the rest.

        # --- targetPort must exist on the matched containers ----------------
        container_ports: set[int] = set()
        port_names: set[str] = set()
        for workload in matched:
            for container in _containers(workload):
                for port in container.get("ports") or []:
                    if not isinstance(port, dict):
                        continue
                    if isinstance(port.get("containerPort"), int):
                        container_ports.add(port["containerPort"])
                    if port.get("name"):
                        port_names.add(str(port["name"]))

        for port in ports:
            if not isinstance(port, dict):
                continue
            target = port.get("targetPort", port.get("port"))
            listed = ", ".join(str(p) for p in sorted(container_ports)) or "none"
            if isinstance(target, int) and container_ports and target not in container_ports:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="TARGET_PORT_MISMATCH",
                        message=(
                            f"{resource.ref} targetPort {target} does not match any containerPort "
                            f"of {matched[0].ref} (declared: {listed})."
                        ),
                        file=str(resource.file),
                        resource=resource.ref,
                        suggestion=(
                            f"Traffic would be sent to a port nothing listens on. Set "
                            f"targetPort to one of: {listed}."
                        ),
                    )
                )
            elif isinstance(target, str) and port_names and target not in port_names:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="TARGET_PORT_NAME_UNKNOWN",
                        message=(
                            f"{resource.ref} targetPort '{target}' is not a declared container "
                            f"port name (known: {', '.join(sorted(port_names)) or 'none'})."
                        ),
                        file=str(resource.file),
                        resource=resource.ref,
                    )
                )

            node_port = port.get("nodePort")
            if node_port is not None:
                if service_type != "NodePort":
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="NODE_PORT_ON_WRONG_TYPE",
                            message=f"{resource.ref} sets nodePort but type is '{service_type}'.",
                            file=str(resource.file),
                            resource=resource.ref,
                            suggestion="Set type: NodePort, or remove nodePort.",
                        )
                    )
                elif not isinstance(node_port, int) or not 30000 <= node_port <= 32767:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="NODE_PORT_OUT_OF_RANGE",
                            message=f"{resource.ref} nodePort {node_port} is outside 30000-32767.",
                            file=str(resource.file),
                            resource=resource.ref,
                        )
                    )



def _check_port_names(
    resource: _Resource,
    names: list[Any],
    where: str,
    issues: list[ValidationIssue],
    max_length: int,
) -> None:
    """Port names must be unique, and valid IANA service names (containers) or DNS labels."""
    present = [str(n) for n in names if n]
    duplicates = sorted({n for n in present if present.count(n) > 1})
    if duplicates:
        issues.append(
            ValidationIssue(
                severity="error",
                code="DUPLICATE_PORT_NAME",
                message=f"{resource.ref} {where} reuses port name(s): {', '.join(duplicates)}.",
                file=str(resource.file),
                resource=resource.ref,
                suggestion="The API server rejects duplicate port names; give each port its own.",
            )
        )
    for name in dict.fromkeys(present):
        valid = (
            len(name) <= max_length
            and bool(_PORT_NAME.match(name))
            and "--" not in name
            and (max_length > 15 or any(c.isalpha() for c in name))
        )
        if not valid:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="INVALID_PORT_NAME",
                    message=(
                        f"{resource.ref} {where} port name '{name}' is invalid: at most "
                        f"{max_length} lowercase alphanumerics or '-', with at least one letter."
                    ),
                    file=str(resource.file),
                    resource=resource.ref,
                )
            )


def _labels_match(selector: dict[str, Any], workload: _Resource) -> bool:
    template = _pod_template(workload)
    if template is None:
        return False
    labels = ((template.get("metadata") or {}).get("labels")) or {}
    return all(labels.get(key) == value for key, value in selector.items())


def _check_references(resources: list[_Resource], issues: list[ValidationIssue]) -> None:
    """ConfigMap / Secret / PVC references must point at something that exists."""
    defined: dict[str, set[str]] = {
        "ConfigMap": {r.name for r in resources if r.kind == "ConfigMap"},
        "Secret": {r.name for r in resources if r.kind == "Secret"},
        "PersistentVolumeClaim": {r.name for r in resources if r.kind == "PersistentVolumeClaim"},
        "Service": {r.name for r in resources if r.kind == "Service"},
    }

    for resource in resources:
        if resource.kind not in WORKLOAD_KINDS:
            continue
        template = _pod_template(resource)
        if template is None:
            continue
        pod_spec = template.get("spec") or {}

        for container in _containers(resource):
            for source in container.get("envFrom") or []:
                if not isinstance(source, dict):
                    continue
                for key, kind in (("configMapRef", "ConfigMap"), ("secretRef", "Secret")):
                    ref = source.get(key)
                    if isinstance(ref, dict) and ref.get("name"):
                        _require(resource, kind, ref["name"], defined, issues, ref.get("optional"))

            for env in container.get("env") or []:
                if not isinstance(env, dict):
                    continue
                value_from = env.get("valueFrom") or {}
                for key, kind in (("configMapKeyRef", "ConfigMap"), ("secretKeyRef", "Secret")):
                    ref = value_from.get(key)
                    if isinstance(ref, dict) and ref.get("name"):
                        _require(resource, kind, ref["name"], defined, issues, ref.get("optional"))

        for volume in pod_spec.get("volumes") or []:
            if not isinstance(volume, dict):
                continue
            if isinstance(volume.get("configMap"), dict) and volume["configMap"].get("name"):
                _require(resource, "ConfigMap", volume["configMap"]["name"], defined, issues, volume["configMap"].get("optional"))
            if isinstance(volume.get("secret"), dict) and volume["secret"].get("secretName"):
                _require(resource, "Secret", volume["secret"]["secretName"], defined, issues, volume["secret"].get("optional"))
            claim = volume.get("persistentVolumeClaim")
            if isinstance(claim, dict) and claim.get("claimName"):
                _require(resource, "PersistentVolumeClaim", claim["claimName"], defined, issues, None)


def _require(
    resource: _Resource,
    kind: str,
    name: str,
    defined: dict[str, set[str]],
    issues: list[ValidationIssue],
    optional: Any,
) -> None:
    if name in defined.get(kind, set()):
        return
    consequence = {
        "ConfigMap": "the pod will fail with CreateContainerConfigError",
        "Secret": "the pod will fail with CreateContainerConfigError",
        "PersistentVolumeClaim": "the pod will stay Pending, unable to mount its volume",
    }.get(kind, "the pod may fail to start")
    issues.append(
        ValidationIssue(
            severity="warning" if optional else "error",
            code=f"MISSING_{kind.upper()}",
            message=(
                f"{resource.ref} references {kind} '{name}', which is not defined in these "
                f"manifests. If it does not already exist in the cluster, {consequence}."
            ),
            file=str(resource.file),
            resource=resource.ref,
            suggestion=f"Generate or create {kind} '{name}' before applying.",
        )
    )


def placeholder_keys(doc: dict[str, Any]) -> list[str]:
    """Keys of a Secret document that still hold the placeholder, in either form."""
    keys: set[str] = set()
    for section, marker in (("stringData", REQUIRED_SECRET), ("data", _PLACEHOLDER_B64)):
        values = doc.get(section)
        if isinstance(values, dict):
            keys.update(str(k) for k, v in values.items() if str(v).strip() == marker)
    return sorted(keys)


def _check_secrets(resources: list[_Resource], issues: list[ValidationIssue]) -> None:
    """Flag placeholder credentials so nobody deploys a Secret full of markers.

    A warning, not an error: generated output is expected to have them, and
    apply_manifests refuses to send them to the cluster.
    """
    for resource in resources:
        if resource.kind != "Secret":
            continue
        placeholders = placeholder_keys(resource.doc)
        if placeholders:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="PLACEHOLDER_SECRET",
                    message=(
                        f"{resource.ref} still holds placeholder value(s) for: "
                        f"{', '.join(sorted(placeholders))}."
                    ),
                    file=str(resource.file),
                    resource=resource.ref,
                    suggestion=(
                        "Replace them with real values, or create the Secret out-of-band with "
                        f"'kubectl create secret generic {resource.name} --from-literal=KEY=VALUE'. "
                        "apply_manifests skips a placeholder Secret that already exists in the "
                        "cluster and refuses to create one."
                    ),
                )
            )


def _valid_name(kind: str, name: str) -> bool:
    """Most kinds take a DNS-1123 subdomain; Services and Namespaces a DNS label."""
    label_rule = _LABEL_NAMED_KINDS.get(kind)
    if label_rule is not None:
        return len(name) <= 63 and bool(label_rule.match(name))
    return len(name) <= 253 and bool(_DNS1123_SUBDOMAIN.match(name))

"""All Kubernetes access lives here.

Reads go through the Kubernetes Python client (structured, typed); writes go
through ``kubectl apply`` because it handles multi-document manifests, ordering
and three-way merges properly.  Every subprocess call is built here as an
explicit argv list -- there is deliberately no way to pass an arbitrary command
through this module.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import get_settings
from ..errors import (
    DeploymentFailed,
    InvalidManifest,
    KubernetesConnectionError,
    KubernetesNotFound,
)
from ..schemas import (
    AppliedResource,
    ApplyResult,
    ContainerState,
    DeploymentStatus,
    Diagnosis,
    EventInfo,
    Finding,
    PodInfo,
    ServiceInfo,
    VerificationCheck,
    VerificationResult,
)
from ..security import resolve_project_path

logger = logging.getLogger(__name__)

#: DNS-1123 label / subdomain, which is also the only shape we pass to kubectl.
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")

#: Container waiting/terminated reasons we can explain to the user.
_KNOWN_PROBLEMS = {
    "CrashLoopBackOff": (
        "The container starts and then exits repeatedly.",
        "Read the container logs (get_pod_logs with previous=true) for the real error.",
    ),
    "ImagePullBackOff": (
        "Kubernetes cannot pull the image.",
        "Check the image name/tag, that it exists in a registry the cluster can reach, "
        "and add imagePullSecrets for private registries. Locally built images need "
        "imagePullPolicy: IfNotPresent or Never.",
    ),
    "ErrImagePull": (
        "Kubernetes cannot pull the image.",
        "Verify the image name and tag, and that the registry is reachable.",
    ),
    "ErrImageNeverPull": (
        "imagePullPolicy is Never and the image is not present on the node.",
        "Build the image on the node, or push it to a registry and relax the pull policy.",
    ),
    "CreateContainerConfigError": (
        "A referenced ConfigMap or Secret key is missing.",
        "Confirm every configMapRef/secretRef exists in this namespace and has the expected keys.",
    ),
    "CreateContainerError": (
        "The container could not be created.",
        "Check the command/args and the container's filesystem expectations.",
    ),
    "InvalidImageName": (
        "The image reference is not a valid name.",
        "Fix the image field in the workload manifest.",
    ),
    "OOMKilled": (
        "The container exceeded its memory limit and was killed.",
        "Raise resources.limits.memory, or reduce the application's memory use.",
    ),
    "ContainerCannotRun": (
        "The container's entrypoint could not be executed.",
        "Check that the command exists in the image and is executable.",
    ),
}


# --------------------------------------------------------------------------
# Client plumbing
# --------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _load_clients() -> tuple[Any, Any, Any]:
    """Load kube config once and return (CoreV1Api, AppsV1Api, ApiClient)."""
    try:
        from kubernetes import client, config  # noqa: PLC0415 - optional at import time
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise KubernetesConnectionError(
            "The 'kubernetes' Python package is not installed.",
            hint="Install it with: pip install kubernetes",
        ) from exc

    settings = get_settings()
    try:
        config.load_kube_config(context=settings.kube_context)
    except Exception:  # noqa: BLE001 - fall back to in-cluster credentials
        try:
            config.load_incluster_config()
        except Exception as exc:  # noqa: BLE001
            raise KubernetesConnectionError(
                "No usable Kubernetes configuration was found.",
                hint=(
                    "Enable Kubernetes in Docker Desktop, then verify with "
                    "'kubectl cluster-info'. Set KUBE_CONTEXT to choose a context."
                ),
            ) from exc

    api_client = client.ApiClient()
    return client.CoreV1Api(api_client), client.AppsV1Api(api_client), api_client


def _core() -> Any:
    return _load_clients()[0]


def _apps() -> Any:
    return _load_clients()[1]


def _validate_name(value: str, what: str = "name") -> str:
    """Reject anything that is not a plain Kubernetes name.

    This is what keeps a crafted 'name' from turning into an extra kubectl flag.
    """
    text = str(value).strip()
    if not text or len(text) > 253 or not _NAME_RE.match(text):
        raise InvalidManifest(
            f"Invalid Kubernetes {what}: '{value}'.",
            hint="Names must be lowercase alphanumeric, '-' or '.', starting and ending alphanumeric.",
        )
    return text


def _namespace(namespace: str | None) -> str:
    return _validate_name(namespace or get_settings().kube_namespace, "namespace")


def _api_error(exc: Exception, what: str) -> Exception:
    """Convert a Kubernetes ApiException into one of our errors."""
    status = getattr(exc, "status", None)
    if status == 404:
        return KubernetesNotFound(f"{what} was not found.", hint="Check the name and namespace.")
    if status in {401, 403}:
        return KubernetesConnectionError(
            f"Not authorised to read {what}.",
            hint="Check your kubeconfig credentials and RBAC permissions.",
        )
    if status is None:
        return KubernetesConnectionError(
            f"Could not reach the Kubernetes API while reading {what}: {exc}",
            hint="Is the cluster running? Verify with 'kubectl cluster-info'.",
        )
    return KubernetesConnectionError(f"Kubernetes API error while reading {what}: {exc}")


async def _call(func: Any, *args: Any, what: str = "resource", **kwargs: Any) -> Any:
    """Run a blocking client call off the event loop and normalise its errors."""
    try:
        return await asyncio.to_thread(functools.partial(func, *args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - re-raised as a domain error
        raise _api_error(exc, what) from exc


# --------------------------------------------------------------------------
# kubectl (writes)
# --------------------------------------------------------------------------
async def _run_kubectl(args: list[str], *, timeout: int | None = None) -> tuple[int, str, str]:
    """Run kubectl with an explicit argv. No shell, no user-supplied commands."""
    settings = get_settings()
    binary = shutil.which(settings.kubectl_path) or settings.kubectl_path
    if shutil.which(binary) is None and not Path(binary).exists():
        raise KubernetesConnectionError(
            f"kubectl was not found at '{settings.kubectl_path}'.",
            hint="Install kubectl or set KUBECTL_PATH to its full path.",
        )

    argv = [binary, *args]
    if settings.kube_context:
        argv.extend(["--context", _validate_name(settings.kube_context, "context")])

    logger.info("Running: kubectl %s", " ".join(args))
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout or settings.kubectl_timeout
        )
    except asyncio.TimeoutError as exc:
        raise DeploymentFailed(
            f"kubectl timed out after {timeout or settings.kubectl_timeout}s.",
            hint="The cluster may be unreachable. Check 'kubectl cluster-info'.",
        ) from exc
    except FileNotFoundError as exc:
        raise KubernetesConnectionError(
            f"kubectl was not found at '{settings.kubectl_path}'.",
            hint="Install kubectl or set KUBECTL_PATH.",
        ) from exc

    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


_APPLY_LINE_RE = re.compile(r"^(?P<kind>[^/\s]+)/(?P<name>\S+)\s+(?P<action>\w+)")


async def apply_manifests(
    path: str,
    namespace: str | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Apply a manifest file or directory. This is the one write operation."""
    target = resolve_project_path(path)
    ns = _namespace(namespace)

    files = _ordered_files(target)
    if not files:
        raise InvalidManifest(
            f"No YAML manifests found at '{target}'.",
            hint="Run generate_manifests first.",
        )

    resources: list[AppliedResource] = []
    output_chunks: list[str] = []

    for file in files:
        args = ["apply", "-f", str(file), "--namespace", ns]
        if dry_run:
            args.append("--dry-run=server")
        code, stdout, stderr = await _run_kubectl(args)
        output_chunks.append(stdout.strip() or stderr.strip())

        if code != 0:
            logger.error("Deployment failed applying %s: %s", file.name, stderr.strip())
            raise DeploymentFailed(
                f"kubectl apply failed on '{file.name}': {stderr.strip() or stdout.strip()}",
                hint=(
                    "Run validate_manifests to catch structural problems, and check that the "
                    f"namespace '{ns}' exists."
                ),
            )

        for line in stdout.splitlines():
            match = _APPLY_LINE_RE.match(line.strip())
            if match:
                resources.append(
                    AppliedResource(
                        kind=match.group("kind").split(".")[0],
                        name=match.group("name"),
                        action=match.group("action"),
                    )
                )

    logger.info(
        "%s %d resource(s) in namespace '%s'",
        "Validated (dry run)" if dry_run else "Applied",
        len(resources),
        ns,
    )
    return ApplyResult(
        applied=not dry_run,
        dry_run=dry_run,
        namespace=ns,
        resources=resources,
        output="\n".join(c for c in output_chunks if c),
        next_step=(
            "Nothing was changed. Re-run with dry_run=false to deploy."
            if dry_run
            else "Call verify_deployment to confirm the rollout succeeded."
        ),
    )


def _ordered_files(target: Path) -> list[Path]:
    """Namespaces first, then everything else alphabetically.

    kubectl does not order files for us, and applying a Deployment into a
    namespace that does not exist yet simply fails.
    """
    if target.is_file():
        return [target]
    files = sorted(p for p in target.rglob("*") if p.suffix in {".yaml", ".yml"} and p.is_file())
    namespaces = [p for p in files if p.name == "namespace.yaml"]
    return namespaces + [p for p in files if p not in namespaces]


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
async def get_deployment_status(name: str, namespace: str | None = None) -> DeploymentStatus:
    """Status of a Deployment, falling back to a StatefulSet of the same name."""
    ns = _namespace(namespace)
    workload_name = _validate_name(name)
    apps = _apps()

    kind = "Deployment"
    try:
        obj = await _call(
            apps.read_namespaced_deployment,
            workload_name,
            ns,
            what=f"Deployment '{workload_name}'",
        )
    except KubernetesNotFound:
        try:
            obj = await _call(
                apps.read_namespaced_stateful_set,
                workload_name,
                ns,
                what=f"StatefulSet '{workload_name}'",
            )
            kind = "StatefulSet"
        except KubernetesNotFound:
            return DeploymentStatus(name=workload_name, namespace=ns, exists=False, healthy=False)

    status = obj.status
    spec = obj.spec
    desired = spec.replicas or 0
    ready = getattr(status, "ready_replicas", None) or 0
    available = getattr(status, "available_replicas", None) or 0
    updated = getattr(status, "updated_replicas", None) or 0
    if kind == "StatefulSet":
        available = getattr(status, "current_replicas", None) or ready
        updated = getattr(status, "updated_replicas", None) or 0

    conditions = [
        f"{c.type}={c.status}" + (f" ({c.reason})" if c.reason else "")
        for c in (getattr(status, "conditions", None) or [])
    ]
    selector = dict((spec.selector.match_labels or {})) if spec.selector else {}

    return DeploymentStatus(
        name=workload_name,
        namespace=ns,
        kind=kind,
        exists=True,
        desired_replicas=desired,
        ready_replicas=ready,
        available_replicas=available,
        updated_replicas=updated,
        conditions=conditions,
        selector=selector,
        healthy=desired > 0 and ready == desired,
    )


async def get_pods(namespace: str | None = None, selector: str | None = None) -> list[PodInfo]:
    """List pods, optionally filtered by a label selector."""
    ns = _namespace(namespace)
    kwargs: dict[str, Any] = {}
    if selector:
        kwargs["label_selector"] = selector
    result = await _call(_core().list_namespaced_pod, ns, what=f"pods in '{ns}'", **kwargs)
    return [_pod_info(pod) for pod in result.items]


def _pod_info(pod: Any) -> PodInfo:
    statuses = pod.status.container_statuses or []
    containers: list[ContainerState] = []
    problems: list[str] = []
    ready_count = 0

    for status in statuses:
        state_name, reason, message, exit_code = _container_state(status.state)
        if status.ready:
            ready_count += 1
        containers.append(
            ContainerState(
                name=status.name,
                ready=bool(status.ready),
                restart_count=status.restart_count or 0,
                state=state_name,
                reason=reason,
                message=message,
                exit_code=exit_code,
            )
        )
        if reason and reason in _KNOWN_PROBLEMS:
            problems.append(reason)
        # A container that restarted after an OOM kill reports it in lastState.
        last = getattr(status, "last_state", None)
        last_terminated = getattr(last, "terminated", None) if last else None
        if last_terminated and getattr(last_terminated, "reason", None) == "OOMKilled":
            problems.append("OOMKilled")

    conditions = [
        f"{c.type}={c.status}" + (f" ({c.reason})" if c.reason else "")
        for c in (pod.status.conditions or [])
    ]
    if pod.status.phase == "Pending":
        problems.append("Pending")

    age = None
    if pod.status.start_time:
        age = int((datetime.now(timezone.utc) - pod.status.start_time).total_seconds())

    return PodInfo(
        name=pod.metadata.name,
        phase=pod.status.phase or "Unknown",
        ready=f"{ready_count}/{len(statuses)}",
        restarts=sum(c.restart_count for c in containers),
        node=pod.spec.node_name,
        ip=pod.status.pod_ip,
        age_seconds=age,
        containers=containers,
        conditions=conditions,
        problems=sorted(set(problems)),
    )


def _container_state(state: Any) -> tuple[str, str | None, str | None, int | None]:
    if state is None:
        return "unknown", None, None, None
    if getattr(state, "running", None):
        return "running", None, None, None
    waiting = getattr(state, "waiting", None)
    if waiting:
        return "waiting", waiting.reason, waiting.message, None
    terminated = getattr(state, "terminated", None)
    if terminated:
        return "terminated", terminated.reason, terminated.message, terminated.exit_code
    return "unknown", None, None, None


async def get_pod_logs(
    pod_name: str,
    namespace: str | None = None,
    container: str | None = None,
    tail_lines: int = 100,
    previous: bool = False,
) -> str:
    """Read logs from a pod. ``previous=True`` reads the crashed instance."""
    ns = _namespace(namespace)
    name = _validate_name(pod_name, "pod name")
    kwargs: dict[str, Any] = {
        "name": name,
        "namespace": ns,
        "tail_lines": max(1, min(int(tail_lines), 2000)),
        "previous": previous,
    }
    if container:
        kwargs["container"] = _validate_name(container, "container name")
    try:
        return await _call(_core().read_namespaced_pod_log, what=f"logs of '{name}'", **kwargs)
    except KubernetesConnectionError as exc:
        if previous:
            # No previous instance simply means the container never restarted.
            return f"(no previous container logs available: {exc.message})"
        raise


async def get_services(namespace: str | None = None, name: str | None = None) -> list[ServiceInfo]:
    """List Services along with how many endpoints each one actually has."""
    ns = _namespace(namespace)
    result = await _call(_core().list_namespaced_service, ns, what=f"services in '{ns}'")
    services = [s for s in result.items if name is None or s.metadata.name == name]

    infos: list[ServiceInfo] = []
    for service in services:
        endpoint_count = await _count_endpoints(service.metadata.name, ns)
        ports = []
        for port in service.spec.ports or []:
            entry = f"{port.port}->{port.target_port}/{port.protocol or 'TCP'}"
            if port.node_port:
                entry += f" nodePort={port.node_port}"
            ports.append(entry)
        infos.append(
            ServiceInfo(
                name=service.metadata.name,
                namespace=ns,
                type=service.spec.type or "ClusterIP",
                cluster_ip=service.spec.cluster_ip,
                ports=ports,
                selector=dict(service.spec.selector or {}),
                endpoint_count=endpoint_count,
                has_endpoints=endpoint_count > 0,
            )
        )
    return infos


async def _count_endpoints(name: str, namespace: str) -> int:
    """Count ready endpoint addresses backing a Service."""
    try:
        endpoints = await _call(
            _core().read_namespaced_endpoints, name, namespace, what=f"endpoints of '{name}'"
        )
    except (KubernetesNotFound, KubernetesConnectionError):
        return 0
    return sum(len(subset.addresses or []) for subset in (endpoints.subsets or []))


async def get_events(namespace: str | None = None, limit: int = 30) -> list[EventInfo]:
    """Recent namespace events, newest last (the order kubectl shows)."""
    ns = _namespace(namespace)
    result = await _call(_core().list_namespaced_event, ns, what=f"events in '{ns}'")

    def sort_key(event: Any) -> datetime:
        stamp = event.last_timestamp or event.event_time or event.first_timestamp
        return stamp or datetime.min.replace(tzinfo=timezone.utc)

    events = sorted(result.items, key=sort_key)[-max(1, min(int(limit), 200)) :]
    return [
        EventInfo(
            type=event.type or "Normal",
            reason=event.reason or "",
            object=f"{event.involved_object.kind}/{event.involved_object.name}"
            if event.involved_object
            else "",
            message=(event.message or "").strip(),
            count=event.count or 1,
            last_seen=str(sort_key(event)) if sort_key(event) != datetime.min.replace(tzinfo=timezone.utc) else None,
        )
        for event in events
    ]


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
async def verify_deployment(
    namespace: str | None = None,
    names: Iterable[str] | None = None,
    health_path: str | None = None,
) -> VerificationResult:
    """Check that what was deployed is actually serving traffic."""
    ns = _namespace(namespace)
    checks: list[VerificationCheck] = []

    workload_names = [_validate_name(n) for n in names] if names else await _managed_workloads(ns)
    if not workload_names:
        return VerificationResult(
            healthy=False,
            namespace=ns,
            summary=f"No workloads found in namespace '{ns}'.",
            next_step="Apply the manifests first, or pass explicit workload names.",
        )

    deployments = [await get_deployment_status(name, ns) for name in workload_names]
    pods = await get_pods(ns)
    services = await get_services(ns)

    for status in deployments:
        checks.append(
            VerificationCheck(
                name=f"{status.kind}/{status.name} exists",
                passed=status.exists,
                detail="Found." if status.exists else "Not found in this namespace.",
            )
        )
        if not status.exists:
            continue
        checks.append(
            VerificationCheck(
                name=f"{status.kind}/{status.name} replicas ready",
                passed=status.healthy,
                detail=f"{status.ready_replicas}/{status.desired_replicas} ready.",
            )
        )

    owned_pods = [p for p in pods if any(p.name.startswith(w + "-") for w in workload_names)]
    running = [p for p in owned_pods if p.phase == "Running"]
    ready_pods = [p for p in owned_pods if _pod_ready(p)]
    restarting = [p for p in owned_pods if p.restarts > 0]

    checks.append(
        VerificationCheck(
            name="pods running",
            passed=bool(owned_pods) and len(running) == len(owned_pods),
            detail=f"{len(running)}/{len(owned_pods)} pod(s) Running.",
        )
    )
    checks.append(
        VerificationCheck(
            name="pods ready",
            passed=bool(owned_pods) and len(ready_pods) == len(owned_pods),
            detail=f"{len(ready_pods)}/{len(owned_pods)} pod(s) passing their readiness probe.",
        )
    )
    checks.append(
        VerificationCheck(
            name="pod restarts",
            passed=not restarting,
            detail=(
                "No restarts."
                if not restarting
                else "Restarted: " + ", ".join(f"{p.name} x{p.restarts}" for p in restarting)
            ),
        )
    )

    relevant_services = [s for s in services if s.name in set(workload_names)]
    for service in relevant_services:
        checks.append(
            VerificationCheck(
                name=f"Service/{service.name} has endpoints",
                passed=service.has_endpoints,
                detail=(
                    f"{service.endpoint_count} endpoint(s)."
                    if service.has_endpoints
                    else "No endpoints: the selector matches no ready pod."
                ),
            )
        )

    if health_path:
        for service in relevant_services:
            checks.append(await _check_health_endpoint(service, ns, health_path))

    healthy = all(check.passed for check in checks)
    summary = _verification_summary(deployments, owned_pods, relevant_services, healthy)
    logger.info("Verification %s in namespace '%s'", "passed" if healthy else "failed", ns)

    return VerificationResult(
        healthy=healthy,
        namespace=ns,
        checks=checks,
        deployments=deployments,
        pods=owned_pods,
        services=relevant_services,
        summary=summary,
        next_step=(
            "Migration verified."
            if healthy
            else "Call diagnose_deployment for the likely cause and a suggested fix."
        ),
    )


def _pod_ready(pod: PodInfo) -> bool:
    return any(c.startswith("Ready=True") for c in pod.conditions)


async def _check_health_endpoint(
    service: ServiceInfo, namespace: str, health_path: str
) -> VerificationCheck:
    """Probe the app through the API server's service proxy.

    This works without port-forwarding or cluster network access, which makes it
    usable from any client on any machine that can reach the API server.
    """
    path = health_path if health_path.startswith("/") else f"/{health_path}"
    if not service.ports:
        return VerificationCheck(
            name=f"health {path}", passed=False, detail="Service exposes no ports."
        )
    port = service.ports[0].split("->")[0]
    try:
        _, _, api_client = _load_clients()
        response = await asyncio.to_thread(
            api_client.call_api,
            f"/api/v1/namespaces/{namespace}/services/{service.name}:{port}/proxy{path}",
            "GET",
            auth_settings=["BearerToken"],
            response_type="str",
            _preload_content=True,
        )
        body = response[0] if isinstance(response, tuple) else response
        return VerificationCheck(
            name=f"health {path}",
            passed=True,
            detail=f"{service.name}{path} responded: {str(body)[:200]}",
        )
    except Exception as exc:  # noqa: BLE001 - the probe is best-effort
        return VerificationCheck(
            name=f"health {path}",
            passed=False,
            detail=f"Could not reach {service.name}{path} via the API proxy: {exc}",
        )


def _verification_summary(
    deployments: list[DeploymentStatus],
    pods: list[PodInfo],
    services: list[ServiceInfo],
    healthy: bool,
) -> str:
    lines = ["Migration successful." if healthy else "Deployment is not healthy."]
    for status in deployments:
        lines.append(
            f"{status.kind}: {status.name} ({status.ready_replicas}/{status.desired_replicas} ready)"
        )
    lines.append(f"Pods: {sum(1 for p in pods if _pod_ready(p))}/{len(pods)} ready")
    for service in services:
        lines.append(
            f"Service: {service.name} ({service.type}, {service.endpoint_count} endpoint(s))"
        )
    return "\n".join(lines)


async def _managed_workloads(namespace: str) -> list[str]:
    """Workloads this tool created, so verification defaults to our own output."""
    apps = _apps()
    selector = "app.kubernetes.io/managed-by=docker2k8s-mcp"
    names: list[str] = []
    for lister, what in (
        (apps.list_namespaced_deployment, "deployments"),
        (apps.list_namespaced_stateful_set, "statefulsets"),
    ):
        result = await _call(lister, namespace, what=what, label_selector=selector)
        names.extend(item.metadata.name for item in result.items)
    return sorted(names)


# --------------------------------------------------------------------------
# Diagnosis
# --------------------------------------------------------------------------
async def diagnose_deployment(
    namespace: str | None = None, names: Iterable[str] | None = None
) -> Diagnosis:
    """Work out why a deployment is unhealthy and what to do about it."""
    ns = _namespace(namespace)
    findings: list[Finding] = []

    workload_names = [_validate_name(n) for n in names] if names else await _managed_workloads(ns)
    deployments = [await get_deployment_status(name, ns) for name in workload_names]
    pods = await get_pods(ns)
    services = await get_services(ns)
    events = await get_events(ns, limit=50)

    owned_pods = [p for p in pods if any(p.name.startswith(w + "-") for w in workload_names)] or pods

    for status in deployments:
        if not status.exists:
            findings.append(
                Finding(
                    problem="Workload missing",
                    resource=f"{status.kind}/{status.name}",
                    evidence=f"No Deployment or StatefulSet named '{status.name}' in '{ns}'.",
                    likely_cause="The manifests were never applied, or applied to another namespace.",
                    suggested_fix="Run apply_manifests, and confirm the namespace matches.",
                )
            )
        elif not status.healthy and status.desired_replicas:
            findings.append(
                Finding(
                    problem="Replicas not ready",
                    resource=f"{status.kind}/{status.name}",
                    evidence=(
                        f"{status.ready_replicas}/{status.desired_replicas} ready. "
                        f"Conditions: {', '.join(status.conditions) or 'none'}."
                    ),
                    likely_cause="At least one pod cannot start or fails its readiness probe.",
                    suggested_fix="Inspect the pod findings below and read the container logs.",
                )
            )

    for pod in owned_pods:
        for container in pod.containers:
            reason = container.reason
            if reason in _KNOWN_PROBLEMS:
                cause, fix = _KNOWN_PROBLEMS[reason]
                findings.append(
                    Finding(
                        problem=reason,
                        resource=f"Pod/{pod.name} container/{container.name}",
                        evidence=(container.message or f"state={container.state}").strip()[:400]
                        + f" (restarts: {container.restart_count})",
                        likely_cause=cause,
                        suggested_fix=fix,
                    )
                )
            elif container.state == "terminated" and container.exit_code:
                findings.append(
                    Finding(
                        problem=f"Container exited with code {container.exit_code}",
                        resource=f"Pod/{pod.name} container/{container.name}",
                        evidence=(container.message or reason or "no message").strip()[:400],
                        likely_cause="The application exited on startup, often a configuration error.",
                        suggested_fix=(
                            f"Read the logs: get_pod_logs(pod_name='{pod.name}', previous=true). "
                            "Missing environment variables are the most common cause."
                        ),
                    )
                )

        if "OOMKilled" in pod.problems:
            cause, fix = _KNOWN_PROBLEMS["OOMKilled"]
            findings.append(
                Finding(
                    problem="OOMKilled",
                    resource=f"Pod/{pod.name}",
                    evidence="A container was previously terminated for exceeding its memory limit.",
                    likely_cause=cause,
                    suggested_fix=fix,
                )
            )

        if pod.phase == "Pending":
            scheduling = [
                e for e in events if e.object == f"Pod/{pod.name}" and e.reason in {"FailedScheduling", "FailedMount"}
            ]
            evidence = scheduling[-1].message if scheduling else "Pod has not been scheduled."
            findings.append(
                Finding(
                    problem="Pending",
                    resource=f"Pod/{pod.name}",
                    evidence=evidence[:400],
                    likely_cause=(
                        "No node satisfies the pod's requests, or a PersistentVolumeClaim "
                        "is unbound."
                    ),
                    suggested_fix=(
                        "Check PVC status and the cluster's default StorageClass, and lower "
                        "resources.requests if the node is too small."
                    ),
                )
            )

        unready = [c for c in pod.containers if not c.ready and c.state == "running"]
        if unready:
            probe_events = [
                e for e in events if e.object == f"Pod/{pod.name}" and e.reason == "Unhealthy"
            ]
            findings.append(
                Finding(
                    problem="Readiness probe failing",
                    resource=f"Pod/{pod.name}",
                    evidence=(
                        probe_events[-1].message
                        if probe_events
                        else f"Container(s) running but not ready: {', '.join(c.name for c in unready)}."
                    )[:400],
                    likely_cause=(
                        "The probe path/port does not match what the application serves, or the "
                        "app is not ready yet (a dependency may be unavailable)."
                    ),
                    suggested_fix=(
                        "Confirm the probe port equals the containerPort the app listens on, and "
                        "that its dependencies are reachable."
                    ),
                )
            )

    for service in services:
        if service.name in set(workload_names) and not service.has_endpoints:
            findings.append(
                Finding(
                    problem="Service has no endpoints",
                    resource=f"Service/{service.name}",
                    evidence=f"selector={service.selector} matched 0 ready pod(s).",
                    likely_cause=(
                        "The selector does not match the pod labels, or no pod is passing its "
                        "readiness probe yet."
                    ),
                    suggested_fix=(
                        "Compare the Service selector with the workload's pod template labels, "
                        "and fix any failing readiness probe first."
                    ),
                )
            )

    for event in events:
        if event.type != "Warning":
            continue
        if event.reason in {"Failed", "FailedCreate", "FailedMount", "BackOff"} and not any(
            f.resource.endswith(event.object.split("/")[-1]) for f in findings
        ):
            findings.append(
                Finding(
                    problem=event.reason,
                    resource=event.object,
                    evidence=event.message[:400],
                    likely_cause="Reported by Kubernetes as a warning event.",
                    suggested_fix="Address the message above; it usually states the exact cause.",
                )
            )

    healthy = not findings
    summary = (
        "No problems detected."
        if healthy
        else f"{len(findings)} problem(s) found: "
        + "; ".join(dict.fromkeys(f.problem for f in findings))
    )
    logger.info("Diagnosis for '%s': %s", ns, summary)

    return Diagnosis(
        namespace=ns,
        healthy=healthy,
        findings=findings,
        recent_events=[e for e in events if e.type == "Warning"][-15:],
        summary=summary,
    )


async def cluster_info() -> dict[str, Any]:
    """Confirm a cluster is reachable before anything else is attempted."""
    core = _core()
    nodes = await _call(core.list_node, what="nodes")
    namespaces = await _call(core.list_namespace, what="namespaces")
    return {
        "reachable": True,
        "node_count": len(nodes.items),
        "nodes": [
            {
                "name": n.metadata.name,
                "ready": any(
                    c.type == "Ready" and c.status == "True" for c in (n.status.conditions or [])
                ),
                "version": n.status.node_info.kubelet_version if n.status.node_info else None,
            }
            for n in nodes.items
        ],
        "namespaces": [n.metadata.name for n in namespaces.items],
    }

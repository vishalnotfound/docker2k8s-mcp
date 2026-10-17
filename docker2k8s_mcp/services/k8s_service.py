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
import heapq
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple

import yaml

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
from ..validators.kubernetes import ManifestBundle, ManifestDoc, placeholder_keys

logger = logging.getLogger(__name__)

#: DNS-1123 label / subdomain, which is also the only shape we pass to kubectl.
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")

#: What apply_manifests may send to a cluster: everything generate_manifests
#: emits plus common namespaced application objects. Nothing cluster-scoped
#: except a Namespace, so a manifest cannot grant itself RBAC or node access.
APPLYABLE_KINDS = frozenset(
    {
        "Namespace",
        "ConfigMap",
        "Secret",
        "Service",
        "Deployment",
        "StatefulSet",
        "PersistentVolumeClaim",
        "Ingress",
        "NetworkPolicy",
        "HorizontalPodAutoscaler",
        "PodDisruptionBudget",
        "Job",
        "CronJob",
    }
)

#: Kinds whose name clashes with another project's object in the namespace.
_OWNED_KINDS = ("Deployment", "StatefulSet", "Service")

#: A restart this recent means the pod is still unstable.
_RESTART_WINDOW_SECONDS = 300
_HEALTH_TIMEOUT_SECONDS = 10
_HEALTH_BODY_BYTES = 200
_LOG_LIMIT_BYTES = 256 * 1024

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
class _Clients(NamedTuple):
    core: Any
    apps: Any
    discovery: Any
    api_client: Any


@functools.lru_cache(maxsize=1)
def _load_clients() -> _Clients:
    """Load kube config once. Blocking: call through ``_clients()``."""
    try:
        from kubernetes import client, config  # noqa: PLC0415 - heavy, import on first use
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise KubernetesConnectionError(
            "The 'kubernetes' Python package is not installed.",
            hint="Install it with: pip install kubernetes",
        ) from exc

    settings = get_settings()
    try:
        # The Kubernetes client treats "" as a literal context name instead
        # of selecting kubeconfig's current-context.
        config.load_kube_config(context=settings.kube_context or None)
    except Exception as exc:  # noqa: BLE001
        if settings.kube_context:
            # An explicit context that fails must not silently become in-cluster auth.
            raise KubernetesConnectionError(
                f"Could not load kube context '{settings.kube_context}': {exc}",
                hint="Compare KUBE_CONTEXT with 'kubectl config get-contexts'.",
            ) from exc
        try:
            config.load_incluster_config()
        except Exception as incluster_exc:  # noqa: BLE001
            raise KubernetesConnectionError(
                f"No usable Kubernetes configuration was found: {exc}",
                hint=(
                    "Enable Kubernetes in Docker Desktop, then verify with "
                    "'kubectl cluster-info'. Set KUBE_CONTEXT to choose a context."
                ),
            ) from incluster_exc

    api_client = client.ApiClient()
    return _Clients(
        client.CoreV1Api(api_client),
        client.AppsV1Api(api_client),
        client.DiscoveryV1Api(api_client),
        api_client,
    )


async def _clients() -> _Clients:
    """Kube config loading reads files and may run an auth plugin: keep it off the loop."""
    return await asyncio.to_thread(_load_clients)


def _validate_name(value: str, what: str = "name") -> str:
    """Reject anything that is not a plain Kubernetes name."""
    text = str(value).strip()
    if not text or len(text) > 253 or not _NAME_RE.match(text):
        raise InvalidManifest(
            f"Invalid Kubernetes {what}: '{value}'.",
            hint="Names must be lowercase alphanumeric, '-' or '.', starting and ending alphanumeric.",
        )
    return text


def _validate_context(value: str) -> str:
    """Context names are free-form (user@cluster, EKS ARNs, gke_project_zone_name).

    kubectl receives it as a single ``--context=<value>`` argument, so it cannot
    become a separate flag; only control characters are refused.
    """
    if not value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise InvalidManifest(
            f"Invalid kube context name: {value!r}.",
            hint="Set KUBE_CONTEXT to a name listed by 'kubectl config get-contexts'.",
        )
    return value


def _namespace(namespace: str | None) -> str:
    return _validate_name(namespace or get_settings().kube_namespace, "namespace")


def _api_error(exc: Any, what: str) -> Exception:
    """Convert a Kubernetes ApiException into one of our errors."""
    status = getattr(exc, "status", None)
    if status == 404:
        return KubernetesNotFound(f"{what} was not found.", hint="Check the name and namespace.")
    if status in {401, 403}:
        return KubernetesConnectionError(
            f"Not authorised to read {what}.",
            hint="Check your kubeconfig credentials and RBAC permissions.",
        )
    if not status:
        return KubernetesConnectionError(
            f"Could not reach the Kubernetes API while reading {what}: {getattr(exc, 'reason', exc)}",
            hint="Is the cluster running? Verify with 'kubectl cluster-info'.",
        )
    return KubernetesConnectionError(
        f"Kubernetes API error while reading {what}: {status} {getattr(exc, 'reason', '')}".strip()
    )


async def _call(func: Any, *args: Any, what: str = "resource", **kwargs: Any) -> Any:
    """Run a blocking client call off the event loop and normalise API errors.

    Only API and transport failures are translated; anything else is a bug in
    this code and propagates with its real type and traceback.
    """
    try:
        return await asyncio.to_thread(functools.partial(func, *args, **kwargs))
    except Exception as exc:
        from kubernetes.client.exceptions import ApiException  # noqa: PLC0415
        from urllib3.exceptions import HTTPError  # noqa: PLC0415

        if isinstance(exc, ApiException):
            raise _api_error(exc, what) from exc
        if isinstance(exc, (HTTPError, OSError)):
            raise KubernetesConnectionError(
                f"Could not reach the Kubernetes API while reading {what}: {exc}",
                hint="Is the cluster running? Verify with 'kubectl cluster-info'.",
            ) from exc
        raise


# --------------------------------------------------------------------------
# kubectl (writes)
# --------------------------------------------------------------------------
async def _run_kubectl(
    args: list[str], *, stdin: bytes | None = None, timeout: int | None = None
) -> tuple[int, str, str]:
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
        argv.append(f"--context={_validate_context(settings.kube_context)}")

    limit = timeout or settings.kubectl_timeout
    logger.info("Running: kubectl %s", " ".join(args))
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise KubernetesConnectionError(
            f"kubectl was not found at '{settings.kubectl_path}'.",
            hint="Install kubectl or set KUBECTL_PATH.",
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=limit)
    except asyncio.TimeoutError as exc:
        process.kill()  # do not leave a kubectl running against the cluster
        await process.wait()
        raise DeploymentFailed(
            f"kubectl timed out after {limit}s.",
            hint="The cluster may be unreachable. Check 'kubectl cluster-info'.",
        ) from exc

    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


_APPLY_LINE_RE = re.compile(r"^(?P<kind>[^/\s]+)/(?P<name>\S+)\s+(?P<action>\w+)")


async def apply_manifests(
    bundle: ManifestBundle,
    namespace: str | None = None,
    dry_run: bool = False,
) -> ApplyResult:
    """Apply already-validated documents. This is the one write operation.

    The documents are streamed to a single ``kubectl apply -f -``, so what runs
    is exactly what was validated. Along the way:

    * objects without a namespace get one, so kubectl never sees a mismatch;
    * a Secret still holding placeholders is skipped when it already exists
      (it must not overwrite real values) and refused when it does not;
    * a Deployment/StatefulSet/Service that another project owns is refused
      instead of failing on an immutable selector;
    * a dry run into a namespace that does not exist yet checks those objects
      client-side, because the API server cannot validate into a missing namespace.
    """
    docs = bundle.resources
    disallowed = sorted({d.ref for d in docs if d.kind not in APPLYABLE_KINDS})
    if disallowed:
        raise InvalidManifest(
            f"apply_manifests does not deploy: {', '.join(disallowed)}.",
            hint=(
                f"Only {', '.join(sorted(APPLYABLE_KINDS))} are applied. Apply anything else "
                "yourself with kubectl after reviewing it."
            ),
        )

    ns = _target_namespace(docs, namespace)
    for doc in docs:
        if doc.kind != "Namespace" and not doc.declared_namespace:
            doc.doc.setdefault("metadata", {})["namespace"] = ns
    # Namespaces first: a namespaced object cannot be created before its namespace.
    docs = sorted(docs, key=lambda d: d.kind != "Namespace")

    created_here = {d.name for d in docs if d.kind == "Namespace"}
    targets = {d.declared_namespace for d in docs if d.kind != "Namespace"}
    exists = await _namespaces_exist(targets | created_here)
    missing = {n for n in targets if not exists.get(n)}
    if missing - created_here and not dry_run:
        raise DeploymentFailed(
            f"Namespace(s) {', '.join(sorted(missing - created_here))} do not exist.",
            hint="Regenerate the manifests (they include namespace.yaml) or create the namespace.",
        )

    skipped, warnings, to_apply = await _screen_secrets(docs, exists, dry_run)
    await _check_ownership(to_apply, exists)

    resources: list[AppliedResource] = []
    output_chunks: list[str] = []
    server_side = [d for d in to_apply if d.kind == "Namespace" or d.declared_namespace not in missing]
    client_side = [d for d in to_apply if d not in server_side]
    if dry_run and client_side:
        warnings.append(
            f"Namespace(s) {', '.join(sorted(missing))} do not exist yet, so {len(client_side)} "
            "object(s) in them were only checked client-side. The API server validates them "
            "on the real deploy."
        )

    batches = [(server_side, "--dry-run=server" if dry_run else None)]
    if client_side:
        batches.append((client_side, "--dry-run=client" if dry_run else None))
    for batch, dry_run_flag in batches:
        if not batch:
            continue
        stdout = await _kubectl_apply(batch, dry_run_flag)
        output_chunks.append(stdout.strip())
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
        skipped=skipped,
        warnings=warnings,
        output="\n".join(c for c in output_chunks if c),
        next_step=(
            "Nothing was changed. Re-run with dry_run=false to deploy."
            if dry_run
            else "Call verify_deployment to confirm the rollout succeeded."
        ),
    )


def _target_namespace(docs: list[ManifestDoc], namespace: str | None) -> str:
    """The namespace to deploy into, refusing a request that contradicts the manifests."""
    declared = {d.declared_namespace for d in docs if d.kind != "Namespace" and d.declared_namespace}
    if namespace:
        ns = _validate_name(namespace, "namespace")
        others = declared - {ns}
        if others:
            raise InvalidManifest(
                f"The manifests target namespace(s) {', '.join(sorted(others))}, not '{ns}'.",
                hint=(
                    "Omit namespace to deploy where the manifests say, or regenerate them "
                    f"with namespace='{ns}'."
                ),
            )
        return ns
    if len(declared) == 1:
        return _validate_name(next(iter(declared)), "namespace")
    for name in declared:
        _validate_name(name, "namespace")
    return _namespace(None)


async def _namespaces_exist(names: set[str]) -> dict[str, bool]:
    clients = await _clients()

    async def check(name: str) -> bool:
        try:
            await _call(clients.core.read_namespace, name, what=f"namespace '{name}'")
            return True
        except KubernetesNotFound:
            return False

    results = await asyncio.gather(*(check(n) for n in sorted(names)))
    return dict(zip(sorted(names), results))


async def _screen_secrets(
    docs: list[ManifestDoc], exists: dict[str, bool], dry_run: bool
) -> tuple[list[str], list[str], list[ManifestDoc]]:
    """Keep placeholder Secrets from overwriting real ones or deploying as-is."""
    clients = await _clients()
    skipped: list[str] = []
    warnings: list[str] = []
    refused: list[str] = []
    to_apply: list[ManifestDoc] = []
    for doc in docs:
        keys = placeholder_keys(doc.doc) if doc.kind == "Secret" else []
        if not keys:
            to_apply.append(doc)
            continue
        ns = doc.declared_namespace or ""
        present = False
        if exists.get(ns):
            try:
                await _call(clients.core.read_namespaced_secret, doc.name, ns, what=f"Secret '{doc.name}'")
                present = True
            except KubernetesNotFound:
                present = False
        if present:
            skipped.append(
                f"Secret/{doc.name}: already exists in '{ns}'; its placeholder values were not "
                "applied over the real ones."
            )
        elif dry_run:
            warnings.append(
                f"Secret/{doc.name} still has placeholders for {', '.join(keys)}; a real deploy "
                "refuses it until they are filled in or the Secret exists."
            )
            to_apply.append(doc)
        else:
            refused.append(f"Secret '{doc.name}' ({doc.file.name}): {', '.join(keys)}")
    if refused:
        raise DeploymentFailed(
            "Refusing to deploy Secrets that still hold <REQUIRED_SECRET> placeholders: "
            + "; ".join(refused)
            + ".",
            hint=(
                "Fill in the values in the Secret file, or create the Secret first with "
                "'kubectl create secret generic <name> -n <namespace> --from-literal=KEY=VALUE', "
                "then apply again."
            ),
        )
    return skipped, warnings, to_apply


async def _check_ownership(docs: list[ManifestDoc], exists: dict[str, bool]) -> None:
    """Refuse to take over an object another project (or no project) created.

    Resource names come from Compose service names, so two projects with an
    'api' in one namespace collide, and kubectl would fail on the immutable
    selector with a much less helpful error.
    """
    clients = await _clients()
    readers = {
        "Deployment": clients.apps.read_namespaced_deployment,
        "StatefulSet": clients.apps.read_namespaced_stateful_set,
        "Service": clients.core.read_namespaced_service,
    }
    part_of = "app.kubernetes.io/part-of"

    async def conflict(doc: ManifestDoc) -> str | None:
        labels = (doc.doc.get("metadata") or {}).get("labels") or {}
        if labels.get("app.kubernetes.io/managed-by") != "docker2k8s-mcp":
            return None
        ns = doc.declared_namespace or ""
        try:
            live = await _call(readers[doc.kind], doc.name, ns, what=doc.ref)
        except KubernetesNotFound:
            return None
        owner = (live.metadata.labels or {}).get(part_of)
        if owner == labels.get(part_of):
            return None
        return f"{doc.ref} in '{ns}' belongs to {f'project {owner!r}' if owner else 'something else'}"

    candidates = [d for d in docs if d.kind in _OWNED_KINDS and exists.get(d.declared_namespace or "")]
    conflicts = [c for c in await asyncio.gather(*(conflict(d) for d in candidates)) if c]
    if conflicts:
        raise DeploymentFailed(
            "These objects already exist and belong to another application: "
            + "; ".join(conflicts)
            + ".",
            hint=(
                "Deploy this project into its own namespace: regenerate the manifests with "
                "namespace='<project-name>' and apply again."
            ),
        )


async def _kubectl_apply(docs: list[ManifestDoc], dry_run_flag: str | None) -> str:
    args = ["apply", "-f", "-"]
    if dry_run_flag:
        args.append(dry_run_flag)
    payload = yaml.safe_dump_all((d.doc for d in docs), sort_keys=False).encode("utf-8")
    code, stdout, stderr = await _run_kubectl(args, stdin=payload)
    if code != 0:
        logger.error("kubectl apply failed: %s", stderr.strip())
        raise DeploymentFailed(
            f"kubectl apply failed: {stderr.strip() or stdout.strip()}",
            hint="Run validate_manifests to catch structural problems, then fix and retry.",
        )
    return stdout


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
async def get_deployment_status(name: str, namespace: str | None = None) -> DeploymentStatus:
    """Status of a Deployment, falling back to a StatefulSet of the same name.

    Healthy means the controller has seen the latest spec and every desired
    replica is updated and ready -- so a rollout still serving from the old
    ReplicaSet while the new pods crash is not reported as healthy.
    """
    ns = _namespace(namespace)
    workload_name = _validate_name(name)
    apps = (await _clients()).apps

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
    desired = spec.replicas if spec.replicas is not None else 1
    ready = getattr(status, "ready_replicas", None) or 0
    available = getattr(status, "available_replicas", None) or 0
    updated = getattr(status, "updated_replicas", None) or 0
    if kind == "StatefulSet":
        available = getattr(status, "available_replicas", None) or ready

    generation = getattr(obj.metadata, "generation", None)
    observed = getattr(status, "observed_generation", None)
    rolled_out = generation is None or (observed is not None and observed >= generation)

    conditions = [
        f"{c.type}={c.status}" + (f" ({c.reason})" if c.reason else "")
        for c in (getattr(status, "conditions", None) or [])
    ]
    if not rolled_out:
        conditions.append("Rollout pending: the controller has not processed the latest spec")
    elif updated < desired:
        conditions.append(f"Rollout in progress: {updated}/{desired} replicas updated")
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
        healthy=rolled_out and ready == desired and updated >= desired and available >= desired,
    )


async def _list_pods(namespace: str, selector: str | None = None) -> list[Any]:
    kwargs: dict[str, Any] = {"label_selector": selector} if selector else {}
    result = await _call(
        (await _clients()).core.list_namespaced_pod, namespace, what=f"pods in '{namespace}'", **kwargs
    )
    return result.items


async def get_pods(namespace: str | None = None, selector: str | None = None) -> list[PodInfo]:
    """List pods, optionally filtered by a label selector."""
    ns = _namespace(namespace)
    now = datetime.now(timezone.utc)
    return [_pod_info(pod, now) for pod in await _list_pods(ns, selector)]


def _pod_info(pod: Any, now: datetime) -> PodInfo:
    statuses = pod.status.container_statuses or []
    containers: list[ContainerState] = []
    problems: list[str] = []
    ready_count = 0

    for status in statuses:
        state_name, reason, message, exit_code = _container_state(status.state)
        if status.ready:
            ready_count += 1
        last = getattr(status, "last_state", None)
        last_terminated = getattr(last, "terminated", None) if last else None
        finished = getattr(last_terminated, "finished_at", None)
        containers.append(
            ContainerState(
                name=status.name,
                ready=bool(status.ready),
                restart_count=status.restart_count or 0,
                state=state_name,
                reason=reason,
                message=message,
                exit_code=exit_code,
                seconds_since_last_restart=int((now - finished).total_seconds()) if finished else None,
            )
        )
        if reason and reason in _KNOWN_PROBLEMS:
            problems.append(reason)
        # A container that restarted after an OOM kill reports it in lastState.
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
        age = int((now - pod.status.start_time).total_seconds())

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


def _owned_pods(pods: list[Any], statuses: list[DeploymentStatus], now: datetime) -> list[PodInfo]:
    """Pods selected by the workloads' own selectors, excluding ones shutting down.

    Matching by selector instead of name prefix keeps 'api' from claiming the
    pods of 'api-worker'.
    """
    selectors = [s.selector for s in statuses if s.exists and s.selector]
    owned = []
    for pod in pods:
        if pod.metadata.deletion_timestamp:
            continue
        labels = pod.metadata.labels or {}
        if any(all(labels.get(k) == v for k, v in sel.items()) for sel in selectors):
            owned.append(_pod_info(pod, now))
    return owned


async def get_pod_logs(
    pod_name: str,
    namespace: str | None = None,
    container: str | None = None,
    tail_lines: int = 100,
    previous: bool = False,
) -> str:
    """Read logs from a pod. ``previous=True`` reads the crashed instance.

    Output is capped at 256 KiB so one noisy container cannot flood the model's
    context or this process's memory.
    """
    ns = _namespace(namespace)
    name = _validate_name(pod_name, "pod name")
    kwargs: dict[str, Any] = {
        "name": name,
        "namespace": ns,
        "tail_lines": max(1, min(int(tail_lines), 2000)),
        "previous": previous,
        "limit_bytes": _LOG_LIMIT_BYTES,
    }
    if container:
        kwargs["container"] = _validate_name(container, "container name")
    try:
        return await _call(
            (await _clients()).core.read_namespaced_pod_log, what=f"logs of '{name}'", **kwargs
        )
    except KubernetesConnectionError as exc:
        if previous:
            # No previous instance simply means the container never restarted.
            return f"(no previous container logs available: {exc.message})"
        raise


async def _list_services(namespace: str, name: str | None = None) -> list[Any]:
    kwargs: dict[str, Any] = {"field_selector": f"metadata.name={name}"} if name else {}
    result = await _call(
        (await _clients()).core.list_namespaced_service,
        namespace,
        what=f"services in '{namespace}'",
        **kwargs,
    )
    return result.items


async def _ready_endpoints(namespace: str, name: str | None = None) -> dict[str, int]:
    """Ready endpoints per Service, from one EndpointSlice list call.

    EndpointSlice replaces the deprecated v1 Endpoints API. A dual-stack Service
    has a slice per address family, so endpoints are counted by pod, not by slice.
    """
    kwargs: dict[str, Any] = {"label_selector": f"kubernetes.io/service-name={name}"} if name else {}
    try:
        result = await _call(
            (await _clients()).discovery.list_namespaced_endpoint_slice,
            namespace,
            what=f"endpoint slices in '{namespace}'",
            **kwargs,
        )
    except (KubernetesNotFound, KubernetesConnectionError):
        return {}
    ready: dict[str, set[str]] = {}
    for endpoint_slice in result.items:
        service = (endpoint_slice.metadata.labels or {}).get("kubernetes.io/service-name")
        if not service:
            continue
        for endpoint in endpoint_slice.endpoints or []:
            conditions = endpoint.conditions
            if conditions is not None and conditions.ready is False:
                continue
            target = endpoint.target_ref.uid if endpoint.target_ref else ",".join(endpoint.addresses or [])
            ready.setdefault(service, set()).add(target)
    return {service: len(targets) for service, targets in ready.items()}


def _service_info(service: Any, namespace: str, endpoints: dict[str, int]) -> ServiceInfo:
    ports = []
    for port in service.spec.ports or []:
        entry = f"{port.port}->{port.target_port}/{port.protocol or 'TCP'}"
        if port.node_port:
            entry += f" nodePort={port.node_port}"
        ports.append(entry)
    count = endpoints.get(service.metadata.name, 0)
    return ServiceInfo(
        name=service.metadata.name,
        namespace=namespace,
        type=service.spec.type or "ClusterIP",
        cluster_ip=service.spec.cluster_ip,
        ports=ports,
        selector=dict(service.spec.selector or {}),
        endpoint_count=count,
        has_endpoints=count > 0,
    )


async def get_services(namespace: str | None = None, name: str | None = None) -> list[ServiceInfo]:
    """List Services along with how many ready endpoints each one actually has."""
    ns = _namespace(namespace)
    if name:
        name = _validate_name(name, "service name")
    services, endpoints = await asyncio.gather(_list_services(ns, name), _ready_endpoints(ns, name))
    return [_service_info(s, ns, endpoints) for s in services]


async def get_events(namespace: str | None = None, limit: int = 30) -> list[EventInfo]:
    """Recent namespace events, newest last (the order kubectl shows)."""
    ns = _namespace(namespace)
    result = await _call((await _clients()).core.list_namespaced_event, ns, what=f"events in '{ns}'")
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def stamp(event: Any) -> datetime:
        return event.last_timestamp or event.event_time or event.first_timestamp or epoch

    # nlargest keeps only `limit` events around instead of sorting the whole list.
    count = max(1, min(int(limit), 200))
    events = sorted(heapq.nlargest(count, result.items, key=stamp), key=stamp)
    return [
        EventInfo(
            type=event.type or "Normal",
            reason=event.reason or "",
            object=f"{event.involved_object.kind}/{event.involved_object.name}"
            if event.involved_object
            else "",
            message=(event.message or "").strip(),
            count=event.count or 1,
            last_seen=str(stamp(event)) if stamp(event) != epoch else None,
        )
        for event in events
    ]


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
async def _statuses(names: list[str], namespace: str) -> list[DeploymentStatus]:
    return list(await asyncio.gather(*(get_deployment_status(n, namespace) for n in names)))


async def verify_deployment(
    namespace: str | None = None,
    names: Iterable[str] | None = None,
    health_path: str | None = None,
    health_services: Iterable[str] | None = None,
) -> VerificationResult:
    """Check that what was deployed is actually serving traffic."""
    ns = _namespace(namespace)
    path = _safe_health_path(health_path) if health_path else None
    checks: list[VerificationCheck] = []

    workload_names = [_validate_name(n) for n in names] if names else await _managed_workloads(ns)
    if not workload_names:
        return VerificationResult(
            healthy=False,
            namespace=ns,
            summary=f"No workloads found in namespace '{ns}'.",
            next_step="Apply the manifests first, or pass explicit workload names.",
        )

    deployments, pods_raw, services_raw, endpoints = await asyncio.gather(
        _statuses(workload_names, ns), _list_pods(ns), _list_services(ns), _ready_endpoints(ns)
    )
    now = datetime.now(timezone.utc)
    owned_pods = _owned_pods(pods_raw, deployments, now)
    del pods_raw  # the raw pod list is the largest object here

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
                detail=(
                    f"{status.ready_replicas}/{status.desired_replicas} ready, "
                    f"{status.updated_replicas}/{status.desired_replicas} updated."
                ),
            )
        )

    expected = sum(s.desired_replicas for s in deployments if s.exists)
    running = [p for p in owned_pods if p.phase == "Running"]
    ready_pods = [p for p in owned_pods if _pod_ready(p)]
    unstable = [p for p in owned_pods if _recently_restarted(p)]
    settled = [p for p in owned_pods if p.restarts and p not in unstable]

    checks.append(
        VerificationCheck(
            name="pods running",
            passed=(bool(owned_pods) or expected == 0) and len(running) == len(owned_pods),
            detail=f"{len(running)}/{len(owned_pods)} pod(s) Running.",
        )
    )
    checks.append(
        VerificationCheck(
            name="pods ready",
            passed=(bool(owned_pods) or expected == 0) and len(ready_pods) == len(owned_pods),
            detail=f"{len(ready_pods)}/{len(owned_pods)} pod(s) passing their readiness probe.",
        )
    )
    detail = (
        "Restarting now: " + ", ".join(f"{p.name} x{p.restarts}" for p in unstable)
        if unstable
        else "No restarts in the last 5 minutes."
    )
    if settled:
        # Early restarts while a dependency starts are normal without depends_on.
        detail += " Earlier restarts, now stable: " + ", ".join(
            f"{p.name} x{p.restarts}" for p in settled
        )
    checks.append(VerificationCheck(name="pod restarts", passed=not unstable, detail=detail))

    wanted = set(workload_names)
    relevant_raw = [s for s in services_raw if s.metadata.name in wanted]
    relevant_services = [_service_info(s, ns, endpoints) for s in relevant_raw]
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

    if path:
        targets = _health_targets(relevant_raw, health_services)
        if not targets:
            checks.append(
                VerificationCheck(
                    name=f"health {path}",
                    passed=False,
                    detail=(
                        "No HTTP Service to probe. Pass health_services with the Service names "
                        "that serve this path."
                    ),
                )
            )
        clients = await _clients() if targets else None
        for service_name, port in targets:
            checks.append(await _check_health_endpoint(clients, service_name, port, ns, path))

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


def _recently_restarted(pod: PodInfo) -> bool:
    return any(
        c.reason == "CrashLoopBackOff"
        or (
            c.seconds_since_last_restart is not None
            and c.seconds_since_last_restart < _RESTART_WINDOW_SECONDS
        )
        for c in pod.containers
    )


def _safe_health_path(path: str) -> str:
    """Refuse anything that could walk out of the service proxy.

    The path is appended to /api/v1/namespaces/<ns>/services/<svc>/proxy, and
    the HTTP client collapses '..', so an unchecked path could reach any API
    server endpoint with the user's credentials.
    """
    text = path.strip()
    if not text.startswith("/"):
        text = f"/{text}"
    route = text.split("?", 1)[0]
    lowered = text.lower()
    if (
        any(segment in {".", ".."} for segment in route.split("/"))
        or any(marker in lowered for marker in ("%2e", "%2f", "%5c", "\\", "#"))
        or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in text)
    ):
        raise InvalidManifest(
            f"Unsafe health_path: {path!r}.",
            hint="Use a plain path such as '/health', without '..', encoded slashes or spaces.",
        )
    return text


def _health_targets(services: list[Any], requested: Iterable[str] | None) -> list[tuple[str, int]]:
    """(service, port) pairs to probe.

    By default only Services that look like HTTP front ends are probed: exposed
    ones, or ones with an http-named port. Probing a database port with an HTTP
    request can only fail.
    """
    wanted = {_validate_name(n, "service name") for n in requested} if requested else None
    targets: list[tuple[str, int]] = []
    for service in services:
        ports = service.spec.ports or []
        if not ports or (wanted is not None and service.metadata.name not in wanted):
            continue
        http = [p for p in ports if (p.name or "").startswith("http")]
        if wanted is None:
            headless = service.spec.cluster_ip == "None"
            exposed = (service.spec.type or "ClusterIP") != "ClusterIP"
            if headless or not (http or exposed):
                continue
        targets.append((service.metadata.name, (http or ports)[0].port))
    return targets


async def _check_health_endpoint(
    clients: Any, service: str, port: int, namespace: str, path: str
) -> VerificationCheck:
    """Probe the app through the API server's service proxy.

    This works without port-forwarding or cluster network access, which makes it
    usable from any client on any machine that can reach the API server. Only
    the first bytes of the body are read.
    """
    name = f"health {service}{path}"

    def fetch() -> bytes:
        response = clients.api_client.call_api(
            f"/api/v1/namespaces/{namespace}/services/{service}:{port}/proxy{path}",
            "GET",
            auth_settings=["BearerToken"],
            _return_http_data_only=True,
            _preload_content=False,
            _request_timeout=_HEALTH_TIMEOUT_SECONDS,
        )
        try:
            return response.read(_HEALTH_BODY_BYTES)
        finally:
            response.release_conn()

    try:
        body = await asyncio.to_thread(fetch)
    except Exception as exc:  # noqa: BLE001 - the probe is best-effort
        status = getattr(exc, "status", None)
        reason = f"HTTP {status} {getattr(exc, 'reason', '')}".strip() if status else str(exc)
        return VerificationCheck(
            name=name, passed=False, detail=f"Could not reach {service}{path} via the API proxy: {reason}"
        )
    return VerificationCheck(
        name=name,
        passed=True,
        detail=f"{service}{path} responded: {body.decode('utf-8', errors='replace')}",
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
    apps = (await _clients()).apps
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
    deployments, pods_raw, services_raw, endpoints, events = await asyncio.gather(
        _statuses(workload_names, ns),
        _list_pods(ns),
        _list_services(ns),
        _ready_endpoints(ns),
        get_events(ns, limit=50),
    )
    now = datetime.now(timezone.utc)
    if workload_names:
        owned_pods = _owned_pods(pods_raw, deployments, now)
    else:
        # Nothing of ours here: diagnose whatever the namespace runs.
        owned_pods = [_pod_info(p, now) for p in pods_raw if not p.metadata.deletion_timestamp]
    del pods_raw
    services = [_service_info(s, ns, endpoints) for s in services_raw]

    if not workload_names and not owned_pods:
        findings.append(
            Finding(
                problem="Nothing deployed",
                resource=f"Namespace/{ns}",
                evidence=f"No workloads created by this tool and no pods in '{ns}'.",
                likely_cause="The manifests were never applied, or applied to another namespace.",
                suggested_fix="Run apply_manifests, and confirm the namespace matches.",
            )
        )

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
                        f"{status.ready_replicas}/{status.desired_replicas} ready, "
                        f"{status.updated_replicas}/{status.desired_replicas} updated. "
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

    wanted = set(workload_names)
    for service in services:
        if service.name in wanted and not service.has_endpoints:
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
    core = (await _clients()).core
    nodes, namespaces = await asyncio.gather(
        _call(core.list_node, what="nodes"), _call(core.list_namespace, what="namespaces")
    )
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

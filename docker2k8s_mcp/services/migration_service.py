"""Turn a Docker project into a reviewable Kubernetes migration plan.

This is where the interesting judgement lives: which Compose concept maps to
which Kubernetes object, what cannot be translated automatically, and what a
human has to decide.  Nothing here touches a cluster.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from ..config import get_settings
from ..schemas import (
    ComposeInspection,
    ComposeService,
    ConceptMapping,
    ContainerPortPlan,
    DockerfileInspection,
    EnvVarPlan,
    MigrationPlan,
    MigrationWarning,
    PortMapping,
    ProbePlan,
    ProjectAnalysis,
    ResourceLimits,
    ServicePlan,
    ServicePortPlan,
    VolumePlan,
)
from ..security import REQUIRED_SECRET
from . import docker_service

logger = logging.getLogger(__name__)

#: Kubernetes NodePort range.
_NODE_PORT_MIN, _NODE_PORT_MAX = 30000, 32767

#: Sensible default request sizes when Compose declares no resource limits.
_DEFAULT_STORAGE_SIZE = "1Gi"

#: Requests for services that declare none. Without any, a pod is BestEffort:
#: first to be evicted and scheduled with no regard for node capacity. Kept
#: small so a laptop cluster still fits everything; no limits are imposed.
_DEFAULT_REQUESTS = ResourceLimits(cpu_request="50m", memory_request="64Mi")


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def analyze_project(path: str, compose: ComposeInspection | None = None) -> ProjectAnalysis:
    """Explain how this project's Docker concepts map onto Kubernetes.

    ``compose`` lets a caller that has already parsed the file reuse it rather
    than paying for a second parse.
    """
    compose = compose or docker_service.inspect_compose(path)
    root = Path(compose.compose_path).parent
    logger.info("Analyzing %d service(s) in %s", len(compose.services), root)

    mappings: list[ConceptMapping] = []
    warnings: list[MigrationWarning] = []
    blockers: list[str] = []
    stateful: list[str] = []
    exposed: list[str] = []

    for service in compose.services:
        name = service.name
        has_storage = any(v.kind == "named" for v in service.volumes)
        if service.stateful:
            detail = "Datastore: needs stable identity and per-pod storage."
        elif has_storage:
            detail = (
                "Stateless workload with persistent storage: a Deployment with a "
                "PersistentVolumeClaim and the Recreate update strategy."
            )
        else:
            detail = "Stateless workload: replicas are interchangeable."
        mappings.append(
            ConceptMapping(
                service=name,
                docker_concept="Compose service / container",
                kubernetes_concept="StatefulSet" if service.stateful else "Deployment",
                detail=detail,
            )
        )
        if service.stateful:
            stateful.append(name)

        for port in service.ports:
            published = bool(port.host_port) and not service.stateful
            mappings.append(
                ConceptMapping(
                    service=name,
                    docker_concept=f"port mapping {port.host_port or ''}:{port.container_port}".strip(":"),
                    kubernetes_concept="Service (NodePort)" if published else "Service (ClusterIP)",
                    detail=(
                        f"Container port {port.container_port} is reached through a Service; "
                        "host port publishing does not exist in Kubernetes."
                    ),
                )
            )
            if published:
                exposed.append(name)

        if service.environment:
            config_keys = [k for k in service.environment if k not in service.secret_keys]
            if config_keys:
                mappings.append(
                    ConceptMapping(
                        service=name,
                        docker_concept="environment variables",
                        kubernetes_concept="ConfigMap",
                        detail=f"{len(config_keys)} non-sensitive variable(s).",
                    )
                )
        if service.secret_keys:
            mappings.append(
                ConceptMapping(
                    service=name,
                    docker_concept="sensitive environment variables",
                    kubernetes_concept="Secret",
                    detail=f"{len(service.secret_keys)} credential(s): {', '.join(service.secret_keys)}.",
                )
            )
            warnings.append(
                MigrationWarning(
                    severity="warning",
                    service=name,
                    code="SECRETS_DETECTED",
                    message=(
                        f"Service '{name}' uses credentials ({', '.join(service.secret_keys)}). "
                        "Generated Secrets contain placeholders, never real values."
                    ),
                    suggestion=(
                        "Fill the placeholders in the generated Secret file, or create the "
                        "Secret with 'kubectl create secret generic' before deploying; "
                        "apply_manifests never overwrites an existing Secret with placeholders."
                    ),
                )
            )

        for volume in service.volumes:
            if volume.kind == "named":
                mappings.append(
                    ConceptMapping(
                        service=name,
                        docker_concept=f"named volume '{volume.source}'",
                        kubernetes_concept="PersistentVolumeClaim",
                        detail=f"Mounted at {volume.target}.",
                    )
                )
            elif volume.kind == "tmpfs":
                mappings.append(
                    ConceptMapping(
                        service=name,
                        docker_concept=f"tmpfs at '{volume.target}'",
                        kubernetes_concept="emptyDir (medium: Memory)",
                        detail="RAM-backed scratch space; counts against the memory limit.",
                    )
                )
            elif volume.kind == "bind":
                warnings.append(
                    MigrationWarning(
                        severity="warning",
                        service=name,
                        code="BIND_MOUNT",
                        message=(
                            f"Service '{name}' bind-mounts '{volume.source}' at "
                            f"'{volume.target}'. Kubernetes has no host bind mount; the "
                            "pod may land on any node."
                        ),
                        suggestion=(
                            "Bake the files into the image, mount them from a ConfigMap "
                            "(small, read-only config), or provision a PersistentVolume."
                        ),
                    )
                )
            elif volume.kind == "anonymous":
                warnings.append(
                    MigrationWarning(
                        severity="info",
                        service=name,
                        code="ANONYMOUS_VOLUME",
                        message=f"Service '{name}' uses an anonymous volume at '{volume.target}'.",
                        suggestion="Rendered as an emptyDir; data is lost when the pod restarts.",
                    )
                )

        if service.healthcheck and not service.healthcheck.disabled:
            mappings.append(
                ConceptMapping(
                    service=name,
                    docker_concept="HEALTHCHECK",
                    kubernetes_concept="readinessProbe + livenessProbe",
                    detail=(
                        "Kubernetes separates 'ready for traffic' from 'still alive'. The "
                        "healthcheck gates traffic; liveness only checks the process is up, "
                        "so an outage in a dependency does not restart every pod."
                    ),
                )
            )
        else:
            warnings.append(
                MigrationWarning(
                    severity="warning",
                    service=name,
                    code="NO_HEALTHCHECK",
                    message=f"Service '{name}' has no healthcheck.",
                    suggestion=(
                        "A TCP readiness probe on the first port is generated when there is "
                        "one. Add an HTTP healthcheck so readiness reflects the app, not just "
                        "an open socket."
                    ),
                )
            )

        if service.depends_on:
            mappings.append(
                ConceptMapping(
                    service=name,
                    docker_concept=f"depends_on: {', '.join(service.depends_on)}",
                    kubernetes_concept="(no equivalent)",
                    detail="Kubernetes starts pods in parallel and relies on retries.",
                )
            )
            warnings.append(
                MigrationWarning(
                    severity="warning",
                    service=name,
                    code="DEPENDS_ON",
                    message=(
                        f"Service '{name}' depends on {', '.join(service.depends_on)}. "
                        "Kubernetes does not order pod startup."
                    ),
                    suggestion=(
                        "Make the app retry its dependencies, or add an initContainer "
                        "that waits for them."
                    ),
                )
            )

        # Compose resolves service names on the default network whether or not
        # networks are declared explicitly, so this mapping always applies.
        mappings.append(
            ConceptMapping(
                service=name,
                docker_concept=(
                    f"service discovery on network(s) {', '.join(service.networks)}"
                    if service.networks
                    else "service discovery on the default network"
                ),
                kubernetes_concept="Service DNS + NetworkPolicy",
                detail=(
                    f"Other pods reach this one at '{_k8s_name(name)}' inside the namespace, "
                    "so Compose service names keep working unchanged. Compose networks "
                    "isolate traffic; in Kubernetes that requires a NetworkPolicy."
                ),
            )
        )

        if service.restart in {"no", None}:
            warnings.append(
                MigrationWarning(
                    severity="info",
                    service=name,
                    code="RESTART_POLICY",
                    message=f"Service '{name}' restart policy is '{service.restart or 'default'}'.",
                    suggestion="Pods in a Deployment always use restartPolicy: Always.",
                )
            )

        if service.privileged:
            blockers.append(
                f"Service '{name}' runs privileged. The manifests keep 'privileged: true', "
                "which needs a cluster (PodSecurity level) that permits it and a human "
                "decision that it is really required."
            )

        if not service.image and not service.build_context:
            blockers.append(f"Service '{name}' declares neither 'image' nor 'build'.")

        if service.build_context:
            warnings.append(
                MigrationWarning(
                    severity="warning",
                    service=name,
                    code="LOCAL_BUILD",
                    message=(
                        f"Service '{name}' is built from source. Kubernetes pulls images from "
                        "a registry and cannot build them."
                    ),
                    suggestion=(
                        "Build the image first (docker build) and push it to a registry, or "
                        "rely on Docker Desktop sharing its image store with the cluster."
                    ),
                )
            )

    for warning in warnings:
        if warning.severity == "error":
            blockers.append(warning.message)

    summary = (
        f"{len(compose.services)} service(s): "
        f"{len(stateful)} stateful, {len(compose.services) - len(stateful)} stateless. "
        f"{len(warnings)} warning(s), {len(blockers)} blocker(s)."
    )

    return ProjectAnalysis(
        project_path=str(root),
        project_name=compose.project_name,
        service_count=len(compose.services),
        services=[s.name for s in compose.services],
        stateful_services=stateful,
        externally_exposed=sorted(set(exposed)),
        mappings=mappings,
        warnings=warnings,
        blockers=blockers,
        summary=summary,
    )


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def create_migration_plan(
    path: str,
    namespace: str | None = None,
    image_registry: str | None = None,
) -> MigrationPlan:
    """Build the concrete plan that ``generate_manifests`` renders.

    ``path`` may be the project directory or the Compose file itself; either
    way the project root is the Compose file's directory, as it is for Compose.
    """
    compose = docker_service.inspect_compose(path)
    root = Path(compose.compose_path).parent
    settings = get_settings()
    namespace = _k8s_name(namespace or settings.kube_namespace, dns1035=False)

    analysis = analyze_project(str(root), compose=compose)
    warnings = list(analysis.warnings)
    blockers = list(analysis.blockers)
    manual_steps: list[str] = []

    if compose.env_source:
        warnings.append(
            MigrationWarning(
                severity="info",
                code="ENV_INTERPOLATION",
                message=f"Compose '${{VAR}}' references were resolved using '{compose.env_source}'.",
                suggestion="Confirm these values are the ones you want in the cluster.",
            )
        )

    dockerfiles = _dockerfile_facts(root, compose)
    shared_claims = _shared_claims(compose, warnings)
    plans = [
        _plan_service(
            service,
            project_name=compose.project_name,
            dockerfile=dockerfiles.get(service.name),
            shared_claims=shared_claims,
            image_registry=image_registry,
            warnings=warnings,
            blockers=blockers,
            manual_steps=manual_steps,
        )
        for service in compose.services
    ]
    defaulted = [s.name for s in compose.services if s.resources is None]
    if defaulted:
        warnings.append(
            MigrationWarning(
                severity="info",
                code="DEFAULT_REQUESTS",
                message=(
                    f"{', '.join(defaulted)} declare no resources; they get requests of "
                    f"{_DEFAULT_REQUESTS.cpu_request} CPU and {_DEFAULT_REQUESTS.memory_request} "
                    "memory and no limits."
                ),
                suggestion="Set deploy.resources in Compose, or edit the manifests, to size them.",
            )
        )

    plan = MigrationPlan(
        project_path=str(root),
        project_name=compose.project_name,
        namespace=namespace,
        services=plans,
        resources=_summarise_resources(plans, namespace),
        ports=_summarise_ports(plans),
        environment_variables=sorted(
            {e.key for p in plans for e in p.env if not e.is_secret}
        ),
        secrets=sorted({e.key for p in plans for e in p.env if e.is_secret}),
        volumes=[
            f"{p.name}: {v.name} -> {v.mount_path} ({v.size_limit or 'memory'} tmpfs)"
            if v.source_kind == "tmpfs"
            else f"{p.name}: {v.name} -> {v.mount_path} ({v.size})"
            for p in plans
            for v in p.volumes
        ],
        healthchecks=[
            f"{p.name}: {p.readiness_probe.kind} readiness"
            + (f" on {p.readiness_probe.path}" if p.readiness_probe.path else "")
            for p in plans
            if p.readiness_probe
        ],
        warnings=warnings,
        blockers=blockers,
        manual_steps=manual_steps,
    )
    logger.info(
        "Migration plan: %d workload(s), %d secret key(s), %d warning(s), %d blocker(s)",
        len(plans),
        len(plan.secrets),
        len(warnings),
        len(blockers),
    )
    return plan


def _plan_service(
    service: ComposeService,
    *,
    project_name: str,
    dockerfile: DockerfileInspection | None,
    shared_claims: dict[str, str],
    image_registry: str | None,
    warnings: list[MigrationWarning],
    blockers: list[str],
    manual_steps: list[str],
) -> ServicePlan:
    name = _k8s_name(service.name)

    # --- image -------------------------------------------------------------
    build_required = service.build_context is not None
    image = service.image or f"{project_name}-{service.name}:local"
    if image_registry and build_required:
        image = f"{image_registry.rstrip('/')}/{image.split('/')[-1]}"
    if "$" in image:
        blockers.append(
            f"Service '{service.name}' image '{image}' contains an unresolved variable; "
            "set it in .env or pass the final image name."
        )
    if build_required:
        manual_steps.append(
            f"Build and make image '{image}' available to the cluster "
            f"(docker build -t {image} {service.build_context})."
        )

    # --- ports -------------------------------------------------------------
    container_ports: list[ContainerPortPlan] = []
    service_ports: list[ServicePortPlan] = []
    port_names = _PortNamer()

    declared = list(service.ports)
    # `expose` publishes nothing on the host but still needs a container port.
    declared_keys = {(p.container_port, p.protocol) for p in declared}
    for expose_port in service.expose:
        if (expose_port, "tcp") not in declared_keys:
            declared.append(PortMapping(container_port=expose_port))

    if not declared and dockerfile is not None:
        # Fall back to the Dockerfile's EXPOSE when Compose declares nothing.
        declared = [PortMapping(container_port=port) for port in dockerfile.exposed_ports]
        if declared:
            warnings.append(
                MigrationWarning(
                    severity="info",
                    service=service.name,
                    code="PORT_FROM_DOCKERFILE",
                    message=(
                        f"Service '{service.name}' declares no Compose ports; using EXPOSE "
                        f"{', '.join(str(p.container_port) for p in declared)} from the Dockerfile."
                    ),
                )
            )

    published = False
    seen_ports: set[tuple[int, str]] = set()
    for mapping in declared:
        protocol = mapping.protocol.upper()
        key = (mapping.container_port, protocol)
        if key in seen_ports:
            continue
        seen_ports.add(key)
        port_name = port_names.name(mapping.container_port, protocol)
        container_ports.append(
            ContainerPortPlan(name=port_name, container_port=mapping.container_port, protocol=protocol)
        )
        node_port: int | None = None
        if mapping.host_port:
            published = True
            if _NODE_PORT_MIN <= mapping.host_port <= _NODE_PORT_MAX:
                node_port = mapping.host_port
        service_ports.append(
            ServicePortPlan(
                name=port_name,
                port=mapping.container_port,
                target_port=mapping.container_port,
                node_port=node_port,
                protocol=protocol,
            )
        )

    # A published database port is a Compose debugging convenience, not
    # something that should become externally reachable in a cluster.
    externally_reachable = published and not service.stateful
    service_type = "NodePort" if externally_reachable else "ClusterIP"
    if published and service.stateful:
        warnings.append(
            MigrationWarning(
                severity="info",
                service=service.name,
                code="DB_PORT_NOT_PUBLISHED",
                message=(
                    f"Service '{service.name}' publishes a host port, but as a datastore it "
                    "was kept internal (ClusterIP)."
                ),
                suggestion=f"Use 'kubectl port-forward svc/{name} <local>:<port>' for ad-hoc access.",
            )
        )
    if externally_reachable and not any(sp.node_port for sp in service_ports):
        host_ports = [m.host_port for m in declared if m.host_port]
        manual_steps.append(
            f"Reach '{service.name}' with: kubectl port-forward svc/{name} "
            f"{host_ports[0] if host_ports else service_ports[0].port}:{service_ports[0].port}"
            " (Compose host ports outside 30000-32767 cannot become NodePorts)."
        )

    # --- environment -------------------------------------------------------
    env = _plan_env(service, warnings)
    has_config = any(not e.is_secret for e in env)
    has_secret = any(e.is_secret for e in env)

    # --- volumes -----------------------------------------------------------
    volumes = _plan_volumes(service, name, shared_claims, manual_steps)
    has_claim = any(v.source_kind == "named" for v in volumes)

    # --- probes ------------------------------------------------------------
    readiness, liveness = _plan_probes(service, container_ports, warnings)
    probe_ports = {p.port for p in (readiness, liveness) if p and p.port}
    for port in sorted(probe_ports - {cp.container_port for cp in container_ports}):
        # Probes reach undeclared ports too, but declaring it keeps the
        # manifest honest and lets validation check the probe.
        container_ports.append(
            ContainerPortPlan(name=port_names.name(port, "TCP"), container_port=port)
        )

    # --- replicas ----------------------------------------------------------
    replicas = service.replicas if service.replicas is not None else 1
    if service.stateful and replicas > 1:
        warnings.append(
            MigrationWarning(
                severity="warning",
                service=service.name,
                code="STATEFUL_REPLICAS",
                message=(
                    f"Service '{service.name}' requests {replicas} replicas but is stateful. "
                    "Scaled to 1; most datastores need explicit clustering configuration."
                ),
                suggestion="Configure replication in the datastore itself before scaling up.",
            )
        )
        replicas = 1
    elif has_claim and not service.stateful and replicas > 1:
        warnings.append(
            MigrationWarning(
                severity="warning",
                service=service.name,
                code="SHARED_RWO_VOLUME",
                message=(
                    f"Service '{service.name}' runs {replicas} replicas on one ReadWriteOnce "
                    "volume; they can only share it while scheduled on the same node."
                ),
                suggestion="Use a ReadWriteMany StorageClass, or run a single replica.",
            )
        )
    if replicas == 0:
        warnings.append(
            MigrationWarning(
                severity="info",
                service=service.name,
                code="ZERO_REPLICAS",
                message=f"Service '{service.name}' has replicas: 0 and will be deployed scaled down.",
            )
        )

    # --- security context --------------------------------------------------
    run_as_user, run_as_non_root, non_root = _plan_user(service, dockerfile, warnings)

    notes = [f"Depends on '{dep}', reachable at DNS name '{_k8s_name(dep)}'." for dep in service.depends_on]

    return ServicePlan(
        name=name,
        image=image,
        build_required=build_required,
        build_context=service.build_context,
        workload_kind="StatefulSet" if service.stateful else "Deployment",
        replicas=replicas,
        strategy="Recreate" if has_claim and not service.stateful else "RollingUpdate",
        image_pull_policy="IfNotPresent",
        command=service.entrypoint,
        args=service.command,
        container_ports=container_ports,
        create_service=bool(service_ports),
        service_type=service_type,  # type: ignore[arg-type]
        headless=service.stateful,
        service_ports=service_ports,
        config_map_name=f"{name}-config" if has_config else None,
        secret_name=f"{name}-secret" if has_secret else None,
        env=env,
        volumes=volumes,
        readiness_probe=readiness,
        liveness_probe=liveness,
        resources=service.resources or _DEFAULT_REQUESTS,
        privileged=service.privileged,
        run_as_user=run_as_user,
        run_as_non_root=run_as_non_root,
        allow_privilege_escalation=False if non_root and not service.privileged else None,
        capabilities_add=service.cap_add,
        capabilities_drop=service.cap_drop,
        depends_on=[_k8s_name(d) for d in service.depends_on],
        notes=notes,
    )


class _PortNamer:
    """Unique, valid port names within one pod (max 15 chars, starts with a letter)."""

    _KNOWN = {
        80: "http",
        443: "https",
        3306: "mysql",
        5432: "postgres",
        6379: "redis",
        8000: "http",
        8080: "http",
        8443: "https",
        27017: "mongo",
    }

    def __init__(self) -> None:
        self.used: set[str] = set()

    def name(self, port: int, protocol: str = "TCP") -> str:
        suffix = "" if protocol == "TCP" else f"-{protocol.lower()}"
        known = self._KNOWN.get(port)
        candidates = [f"{known}{suffix}", f"{known}-{port}{suffix}"] if known else []
        candidates.append(f"port-{port}{suffix}")
        for candidate in candidates:
            if len(candidate) <= 15 and candidate not in self.used:
                self.used.add(candidate)
                return candidate
        fallback = f"p{port}{suffix}"[:15]
        self.used.add(fallback)
        return fallback


def _plan_env(service: ComposeService, warnings: list[MigrationWarning]) -> list[EnvVarPlan]:
    """Split the already-interpolated environment into config vs secret.

    Secret values are replaced with a placeholder here so a real credential can
    never reach a generated manifest.
    """
    secret_keys = set(service.secret_keys)
    env = [
        EnvVarPlan(key=key, value=REQUIRED_SECRET, is_secret=True, source="secret")
        if key in secret_keys
        else EnvVarPlan(key=key, value=value if value is not None else "", source="configmap")
        for key, value in service.environment.items()
    ]

    if service.unresolved_variables:
        warnings.append(
            MigrationWarning(
                severity="warning",
                service=service.name,
                code="UNRESOLVED_INTERPOLATION",
                message=(
                    f"Service '{service.name}' references variable(s) with no usable value: "
                    f"{', '.join(service.unresolved_variables)}."
                ),
                suggestion=(
                    "Define them in .env. References to secret variables are never resolved; "
                    "values that use them are routed to the Secret instead."
                ),
            )
        )
    if service.env_files:
        warnings.append(
            MigrationWarning(
                severity="info",
                service=service.name,
                code="ENV_FILE",
                message=(
                    f"Service '{service.name}' loads {', '.join(service.env_files)}; those keys "
                    "are not automatically added to the ConfigMap."
                ),
                suggestion=(
                    "Create a ConfigMap from the file if needed: "
                    f"kubectl create configmap {_k8s_name(service.name)}-envfile "
                    f"--from-env-file={service.env_files[0]}"
                ),
            )
        )

    env.sort(key=lambda e: e.key)
    return env


def _plan_volumes(
    service: ComposeService,
    name: str,
    shared_claims: dict[str, str],
    manual_steps: list[str],
) -> list[VolumePlan]:
    volumes: list[VolumePlan] = []
    used: set[str] = set()

    def unique(base: str) -> str:
        candidate, n = _k8s_name(base), 2
        while candidate in used:
            candidate = _k8s_name(f"{base}-{n}")
            n += 1
        used.add(candidate)
        return candidate

    for volume in service.volumes:
        if volume.kind == "named":
            vol_name = unique(volume.source or f"{service.name}-data")
            claim = None
            if not service.stateful:
                claim = shared_claims.get(volume.source or "") or _k8s_name(f"{name}-{vol_name}")
            volumes.append(
                VolumePlan(
                    name=vol_name,
                    mount_path=volume.target,
                    size=_DEFAULT_STORAGE_SIZE,
                    read_only=volume.read_only,
                    source_kind="named",
                    claim_name=claim,
                )
            )
        elif volume.kind == "bind":
            manual_steps.append(
                f"Decide how to provide '{volume.source}' (mounted at '{volume.target}' in "
                f"'{service.name}'): bake into the image, use a ConfigMap, or a PersistentVolume."
            )
        elif volume.kind == "anonymous":
            volumes.append(
                VolumePlan(
                    name=unique(f"{service.name}-{Path(volume.target).name or 'data'}"),
                    mount_path=volume.target,
                    source_kind="anonymous",
                )
            )
        elif volume.kind == "tmpfs":
            volumes.append(
                VolumePlan(
                    name=unique(f"tmpfs-{Path(volume.target).name or 'data'}"),
                    mount_path=volume.target,
                    source_kind="tmpfs",
                    size_limit=volume.size,
                )
            )
    return volumes


def _shared_claims(compose: ComposeInspection, warnings: list[MigrationWarning]) -> dict[str, str]:
    """Compose volumes used by several services -> one shared PVC name.

    Compose shares a named volume between every service that mounts it. Two
    Deployments can share one PVC; a StatefulSet always gets per-pod storage,
    so sharing with it cannot be preserved.
    """
    users: dict[str, list[ComposeService]] = defaultdict(list)
    for service in compose.services:
        for volume in {v.source for v in service.volumes if v.kind == "named" and v.source}:
            users[volume].append(service)

    shared: dict[str, str] = {}
    for volume, services in users.items():
        if len(services) < 2:
            continue
        names = ", ".join(s.name for s in services)
        if any(s.stateful for s in services):
            warnings.append(
                MigrationWarning(
                    severity="warning",
                    code="SHARED_VOLUME_NOT_PRESERVED",
                    message=(
                        f"Volume '{volume}' is shared by {names}, but a StatefulSet gets its own "
                        "storage, so the services will no longer see the same data."
                    ),
                    suggestion="Share the data through the datastore's API instead of its files.",
                )
            )
            continue
        shared[volume] = _k8s_name(f"{compose.project_name}-{volume}")
        warnings.append(
            MigrationWarning(
                severity="warning",
                code="SHARED_VOLUME",
                message=(
                    f"Volume '{volume}' is shared by {names}; they mount one PVC "
                    f"'{shared[volume]}'. With ReadWriteOnce storage their pods must be "
                    "scheduled on the same node."
                ),
                suggestion="Use a ReadWriteMany StorageClass on multi-node clusters.",
            )
        )
    return shared


def _plan_user(
    service: ComposeService,
    dockerfile: DockerfileInspection | None,
    warnings: list[MigrationWarning],
) -> tuple[int | None, bool, bool]:
    """Work out (runAsUser, runAsNonRoot, runs-as-non-root) for the container.

    Compose's ``user:`` overrides the image and must become runAsUser. The
    Dockerfile's USER is already baked into the image, so it only informs
    runAsNonRoot -- and only when numeric, because the kubelet refuses to start
    a runAsNonRoot pod whose image user is a name it cannot verify.
    """
    compose_user = (service.user or "").split(":", 1)[0].strip()
    if compose_user:
        if compose_user.isdigit():
            uid = int(compose_user)
            return uid, uid != 0, uid != 0
        if compose_user != "root":
            warnings.append(
                MigrationWarning(
                    severity="info",
                    service=service.name,
                    code="USER_BY_NAME",
                    message=(
                        f"Service '{service.name}' sets user '{service.user}' by name; "
                        "Kubernetes runAsUser needs a numeric UID."
                    ),
                    suggestion="Set securityContext.runAsUser to that user's UID.",
                )
            )
            return None, False, True
        return None, False, False

    image_user = ((dockerfile.user if dockerfile else None) or "").split(":", 1)[0].strip()
    if image_user.isdigit():
        return None, image_user != "0", image_user != "0"
    return None, False, bool(image_user) and image_user != "root"


def _plan_probes(
    service: ComposeService,
    container_ports: list[ContainerPortPlan],
    warnings: list[MigrationWarning],
) -> tuple[ProbePlan | None, ProbePlan | None]:
    """Translate a Compose healthcheck into readiness + liveness probes.

    The healthcheck gates traffic (readiness). Liveness restarts the container,
    so it only checks what the container controls: an HTTP healthcheck often
    also checks the database, and a liveness probe on it would restart every
    pod whenever the database blips.
    """
    health = service.healthcheck
    tcp_port = next((p.container_port for p in container_ports if p.protocol == "TCP"), None)

    if health is None or not health.test:
        if health is None and tcp_port is not None:
            # No healthcheck: at least wait for the socket before sending traffic.
            return ProbePlan(kind="tcpSocket", port=tcp_port, initial_delay_seconds=5), None
        return None, None
    if health.disabled:
        return None, None

    test = list(health.test)
    mode = test[0].upper()
    command: list[str]
    if mode == "NONE":
        return None, None
    if mode == "CMD":
        command = test[1:]
    elif mode == "CMD-SHELL":
        command = ["/bin/sh", "-c", " ".join(test[1:])]
    else:
        command = ["/bin/sh", "-c", " ".join(test)]

    period = health.interval_seconds or 10
    timeout = health.timeout_seconds or 5
    retries = health.retries or 3
    start_period = health.start_period_seconds or 10

    http = _http_probe_from_command(command)
    if http is not None:
        path, port, scheme = http
        readiness = ProbePlan(
            kind="httpGet",
            path=path,
            port=port,
            scheme="HTTPS" if scheme == "https" else None,
            initial_delay_seconds=min(start_period, 10),
            period_seconds=period,
            timeout_seconds=timeout,
            failure_threshold=retries,
        )
        liveness = ProbePlan(
            kind="tcpSocket",
            port=port,
            initial_delay_seconds=start_period,
            period_seconds=max(period, 15),
            timeout_seconds=timeout,
            # Liveness restarts the pod, so make it slower to fire than readiness.
            failure_threshold=max(retries, 3),
        )
        return readiness, liveness

    readiness = ProbePlan(
        kind="exec",
        command=command,
        initial_delay_seconds=min(start_period, 10),
        period_seconds=period,
        timeout_seconds=timeout,
        failure_threshold=retries,
    )
    liveness = readiness.model_copy(
        update={
            "initial_delay_seconds": start_period,
            "period_seconds": max(period, 15),
            "failure_threshold": max(retries, 3),
        }
    )
    return readiness, liveness


_URL_RE = re.compile(
    r"(?P<scheme>https?)://(?P<host>\[[0-9a-fA-F:]+\]|[^/:\s\"'|;&)]+)(?::(?P<port>\d+))?"
    r"(?P<path>/[^\s\"'|;&)]*)?"
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "[::]"}


def _http_probe_from_command(command: Iterable[str]) -> tuple[str, int, str] | None:
    """Recognise `curl`/`wget` healthchecks so they become real httpGet probes.

    Returns (path, port, scheme). A URL without a port means the scheme's
    default, exactly as curl would use it.
    """
    tokens = list(command)
    if not tokens:
        return None
    binary = Path(tokens[0]).name.lower()
    joined = " ".join(tokens)
    if binary not in {"curl", "wget"} and not re.search(r"\b(curl|wget)\b", joined):
        return None
    match = _URL_RE.search(joined)
    if not match:
        return None
    # Only localhost URLs describe the container itself.
    if match.group("host").lower() not in _LOCAL_HOSTS:
        return None
    scheme = match.group("scheme")
    port = int(match.group("port")) if match.group("port") else (443 if scheme == "https" else 80)
    return match.group("path") or "/", port, scheme


def _dockerfile_facts(root: Path, compose: ComposeInspection) -> dict[str, DockerfileInspection]:
    """Map Compose service name -> its parsed Dockerfile (for EXPOSE and USER)."""
    result: dict[str, DockerfileInspection] = {}
    for service in compose.services:
        context_ref = service.build_context
        if not context_ref or "://" in context_ref or context_ref.startswith("git@"):
            continue  # remote build contexts cannot be read
        context = (root / context_ref).resolve()
        try:
            if service.dockerfile:
                dockerfile: Path | None = context / service.dockerfile
            else:
                dockerfile = docker_service.find_dockerfile(context) if context.is_dir() else None
            if dockerfile is None or not dockerfile.is_file():
                continue
            result[service.name] = docker_service.inspect_dockerfile(str(dockerfile))
        except Exception:  # noqa: BLE001 - a broken or out-of-sandbox Dockerfile must not stop planning
            logger.warning("Could not read the Dockerfile for service '%s'", service.name)
    return result


def _summarise_resources(plans: list[ServicePlan], namespace: str) -> list[str]:
    resources = [f"Namespace/{namespace}"]
    claims: dict[str, None] = {}
    for plan in plans:
        resources.append(f"{plan.workload_kind}/{plan.name}")
        if plan.create_service:
            resources.append(f"Service/{plan.name}")
        if plan.config_map_name:
            resources.append(f"ConfigMap/{plan.config_map_name}")
        if plan.secret_name:
            resources.append(f"Secret/{plan.secret_name}")
        for volume in plan.volumes:
            if volume.source_kind != "named":
                continue
            if plan.workload_kind == "StatefulSet":
                resources.append(
                    f"PersistentVolumeClaim/{volume.name}-{plan.name}-<ordinal> (volumeClaimTemplate)"
                )
            elif volume.claim_name:
                claims.setdefault(f"PersistentVolumeClaim/{volume.claim_name}")
    return resources + list(claims)


def _summarise_ports(plans: list[ServicePlan]) -> list[str]:
    summary: list[str] = []
    for plan in plans:
        for port in plan.service_ports:
            entry = f"{plan.name}: {plan.service_type} {port.port} -> container {port.target_port}"
            if port.node_port:
                entry += f" (nodePort {port.node_port})"
            summary.append(entry)
    return summary


def _k8s_name(name: str, *, dns1035: bool = True) -> str:
    """Coerce a name into a DNS label.

    Resource names default to DNS-1035 (must start with a letter) because a
    Service requires it and every resource shares its service's name.
    Namespaces only need DNS-1123, which may start with a digit.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-")
    if dns1035 and slug and not slug[0].isalpha():
        slug = f"s-{slug}"
    return (slug or "app")[:63].rstrip("-")

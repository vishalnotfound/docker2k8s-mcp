"""Turn a Docker project into a reviewable Kubernetes migration plan.

This is where the interesting judgement lives: which Compose concept maps to
which Kubernetes object, what cannot be translated automatically, and what a
human has to decide.  Nothing here touches a cluster.
"""

from __future__ import annotations

import logging
import re
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
    ServicePlan,
    ServicePortPlan,
    VolumePlan,
)
from ..security import REQUIRED_SECRET, classify_env, resolve_project_path
from . import docker_service

logger = logging.getLogger(__name__)

#: Compose ${VAR}, ${VAR:-default} and ${VAR-default}.
_INTERPOLATION_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::?-(?P<default>[^}]*))?\}")

#: Kubernetes NodePort range.
_NODE_PORT_MIN, _NODE_PORT_MAX = 30000, 32767

#: Sensible default request sizes when Compose declares no resource limits.
_DEFAULT_STORAGE_SIZE = "1Gi"


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def analyze_project(path: str, compose: ComposeInspection | None = None) -> ProjectAnalysis:
    """Explain how this project's Docker concepts map onto Kubernetes.

    ``compose`` lets a caller that has already parsed the file reuse it rather
    than paying for a second parse.
    """
    root = resolve_project_path(path)
    compose = compose or docker_service.inspect_compose(str(root))
    logger.info("Analyzing %d service(s) in %s", len(compose.services), root)

    mappings: list[ConceptMapping] = []
    warnings: list[MigrationWarning] = []
    blockers: list[str] = []
    stateful: list[str] = []
    exposed: list[str] = []

    for service in compose.services:
        name = service.name
        mappings.append(
            ConceptMapping(
                service=name,
                docker_concept="Compose service / container",
                kubernetes_concept="StatefulSet" if service.stateful else "Deployment",
                detail=(
                    "Stateful workload: needs stable identity and storage."
                    if service.stateful
                    else "Stateless workload: replicas are interchangeable."
                ),
            )
        )
        if service.stateful:
            stateful.append(name)

        for port in service.ports:
            target = "Service (NodePort)" if port.host_port else "Service (ClusterIP)"
            mappings.append(
                ConceptMapping(
                    service=name,
                    docker_concept=f"port mapping {port.host_port or ''}:{port.container_port}".strip(":"),
                    kubernetes_concept=target,
                    detail=(
                        f"Container port {port.container_port} is reached through a Service; "
                        "host port publishing does not exist in Kubernetes."
                    ),
                )
            )
            if port.host_port:
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
                        "Fill the Secret with real values via 'kubectl create secret generic' "
                        "or a secrets manager before deploying."
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
                    detail="Kubernetes separates 'ready for traffic' from 'still alive'.",
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
                        "Without a readinessProbe, Kubernetes sends traffic as soon as the "
                        "process starts, before the app can serve it."
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
                f"Service '{name}' runs privileged; this needs an explicit securityContext "
                "and a cluster that permits it."
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
    """Build the concrete plan that ``generate_manifests`` renders."""
    root = resolve_project_path(path)
    compose = docker_service.inspect_compose(str(root))
    settings = get_settings()
    namespace = _k8s_name(namespace or settings.kube_namespace)

    env_defaults, env_source = _load_interpolation_values(root)
    analysis = analyze_project(str(root), compose=compose)
    warnings = list(analysis.warnings)
    manual_steps: list[str] = []

    if env_source:
        warnings.append(
            MigrationWarning(
                severity="info",
                code="ENV_INTERPOLATION",
                message=f"Compose '${{VAR}}' references were resolved using '{env_source}'.",
                suggestion="Confirm these values are the ones you want in the cluster.",
            )
        )

    dockerfiles = _dockerfile_ports(root, compose)
    plans = [
        _plan_service(
            service,
            project_name=compose.project_name,
            env_defaults=env_defaults,
            dockerfile_ports=dockerfiles,
            image_registry=image_registry,
            warnings=warnings,
            manual_steps=manual_steps,
        )
        for service in compose.services
    ]

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
        volumes=[f"{p.name}: {v.name} -> {v.mount_path} ({v.size})" for p in plans for v in p.volumes],
        healthchecks=[
            f"{p.name}: {p.readiness_probe.kind} readiness"
            + (f" on {p.readiness_probe.path}" if p.readiness_probe.path else "")
            for p in plans
            if p.readiness_probe
        ],
        warnings=warnings,
        manual_steps=manual_steps,
    )
    logger.info(
        "Migration plan: %d workload(s), %d secret key(s), %d warning(s)",
        len(plans),
        len(plan.secrets),
        len(warnings),
    )
    return plan


def _plan_service(
    service: ComposeService,
    *,
    project_name: str,
    env_defaults: dict[str, str],
    dockerfile_ports: dict[str, list[int]],
    image_registry: str | None,
    warnings: list[MigrationWarning],
    manual_steps: list[str],
) -> ServicePlan:
    name = _k8s_name(service.name)

    # --- image -------------------------------------------------------------
    build_required = service.build_context is not None
    image = service.image or f"{project_name}-{service.name}:local"
    if image_registry and build_required:
        image = f"{image_registry.rstrip('/')}/{image.split('/')[-1]}"
    if build_required:
        manual_steps.append(
            f"Build and make image '{image}' available to the cluster "
            f"(docker build -t {image} {service.build_context})."
        )

    # --- ports -------------------------------------------------------------
    container_ports: list[ContainerPortPlan] = []
    service_ports: list[ServicePortPlan] = []
    seen_ports: set[int] = set()

    declared = list(service.ports)
    # `expose` publishes nothing on the host but still needs a container port.
    for expose_port in service.expose:
        if expose_port not in {p.container_port for p in declared}:
            declared.append(PortMapping(container_port=expose_port))

    if not declared:
        # Fall back to the Dockerfile's EXPOSE when Compose declares nothing.
        for port in dockerfile_ports.get(service.name, []):
            container_ports.append(ContainerPortPlan(name=_port_name(port), container_port=port))
            seen_ports.add(port)
        if container_ports:
            warnings.append(
                MigrationWarning(
                    severity="info",
                    service=service.name,
                    code="PORT_FROM_DOCKERFILE",
                    message=(
                        f"Service '{service.name}' declares no Compose ports; using EXPOSE "
                        f"{container_ports[0].container_port} from the Dockerfile."
                    ),
                )
            )

    published = False
    for mapping in declared:
        port = mapping.container_port
        if port in seen_ports:
            continue
        seen_ports.add(port)
        container_ports.append(
            ContainerPortPlan(
                name=_port_name(port),
                container_port=port,
                protocol=mapping.protocol.upper(),
            )
        )
        node_port: int | None = None
        host_port = getattr(mapping, "host_port", None)
        if host_port:
            published = True
            if _NODE_PORT_MIN <= host_port <= _NODE_PORT_MAX:
                node_port = host_port
        service_ports.append(
            ServicePortPlan(
                name=_port_name(port),
                port=port,
                target_port=port,
                node_port=node_port,
                protocol=mapping.protocol.upper(),
            )
        )

    if not service_ports and container_ports:
        service_ports = [
            ServicePortPlan(name=cp.name, port=cp.container_port, target_port=cp.container_port)
            for cp in container_ports
        ]

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
        host_ports = [m.host_port for m in declared if getattr(m, "host_port", None)]
        manual_steps.append(
            f"Reach '{service.name}' with: kubectl port-forward svc/{name} "
            f"{host_ports[0] if host_ports else service_ports[0].port}:{service_ports[0].port}"
            " (Compose host ports outside 30000-32767 cannot become NodePorts)."
        )

    # --- environment -------------------------------------------------------
    env, secret_keys = _plan_env(service, env_defaults, warnings)
    config_keys = [e for e in env if not e.is_secret]

    # --- volumes -----------------------------------------------------------
    volumes: list[VolumePlan] = []
    for volume in service.volumes:
        if volume.kind == "named":
            volumes.append(
                VolumePlan(
                    name=_k8s_name(volume.source or f"{service.name}-data"),
                    mount_path=volume.target,
                    size=_DEFAULT_STORAGE_SIZE,
                    read_only=volume.read_only,
                    source_kind="named",
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
                    name=_k8s_name(f"{service.name}-{Path(volume.target).name or 'data'}"),
                    mount_path=volume.target,
                    source_kind="anonymous",
                )
            )

    # --- probes ------------------------------------------------------------
    readiness, liveness = _plan_probes(service, container_ports, warnings)

    # --- replicas ----------------------------------------------------------
    replicas = service.replicas or 1
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

    notes: list[str] = []
    for dep in service.depends_on:
        notes.append(f"Depends on '{dep}', reachable at DNS name '{_k8s_name(dep)}'.")

    return ServicePlan(
        name=name,
        image=image,
        build_required=build_required,
        build_context=service.build_context,
        workload_kind="StatefulSet" if service.stateful else "Deployment",
        replicas=replicas,
        image_pull_policy="IfNotPresent" if build_required else "IfNotPresent",
        command=service.entrypoint,
        args=service.command,
        container_ports=container_ports,
        create_service=bool(service_ports),
        service_type=service_type,  # type: ignore[arg-type]
        headless=service.stateful,
        service_ports=service_ports,
        config_map_name=f"{name}-config" if config_keys else None,
        secret_name=f"{name}-secret" if secret_keys else None,
        env=env,
        volumes=volumes,
        readiness_probe=readiness,
        liveness_probe=liveness,
        resources=service.resources,
        depends_on=[_k8s_name(d) for d in service.depends_on],
        notes=notes,
    )


def _plan_env(
    service: ComposeService,
    env_defaults: dict[str, str],
    warnings: list[MigrationWarning],
) -> tuple[list[EnvVarPlan], list[str]]:
    """Resolve Compose interpolation and split variables into config vs secret.

    Secret values are replaced with a placeholder here so a real credential can
    never reach a generated manifest.
    """
    env: list[EnvVarPlan] = []
    secret_keys: list[str] = []
    unresolved: list[str] = []

    for key, raw_value in service.environment.items():
        is_secret = key in service.secret_keys
        if is_secret:
            secret_keys.append(key)
            env.append(EnvVarPlan(key=key, value=REQUIRED_SECRET, is_secret=True, source="secret"))
            continue

        value, missing = _interpolate(raw_value, env_defaults)
        unresolved.extend(missing)
        # Interpolation can reveal that a bland-looking key holds a credential.
        if value is not None and classify_env(key, value):
            secret_keys.append(key)
            env.append(EnvVarPlan(key=key, value=REQUIRED_SECRET, is_secret=True, source="secret"))
            continue
        env.append(EnvVarPlan(key=key, value=value if value is not None else "", source="configmap"))

    if unresolved:
        warnings.append(
            MigrationWarning(
                severity="warning",
                service=service.name,
                code="UNRESOLVED_INTERPOLATION",
                message=(
                    f"Service '{service.name}' references undefined variable(s): "
                    f"{', '.join(sorted(set(unresolved)))}."
                ),
                suggestion="Set them in .env, or edit the generated ConfigMap before deploying.",
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
    return env, sorted(set(secret_keys))


def _plan_probes(
    service: ComposeService,
    container_ports: list[ContainerPortPlan],
    warnings: list[MigrationWarning],
) -> tuple[ProbePlan | None, ProbePlan | None]:
    """Translate a Compose HEALTHCHECK into readiness + liveness probes."""
    health = service.healthcheck
    if health is None or health.disabled or not health.test:
        return None, None

    test = list(health.test)
    mode = test[0].upper() if test else ""
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
        path, port = http
        if port is None:
            port = container_ports[0].container_port if container_ports else None
        if port is not None:
            readiness = ProbePlan(
                kind="httpGet",
                path=path,
                port=port,
                initial_delay_seconds=min(start_period, 10),
                period_seconds=period,
                timeout_seconds=timeout,
                failure_threshold=retries,
            )
            liveness = readiness.model_copy(
                update={
                    "initial_delay_seconds": start_period,
                    "period_seconds": max(period, 15),
                    # Liveness restarts the pod, so make it slower to fire than readiness.
                    "failure_threshold": max(retries, 3),
                }
            )
            return readiness, liveness
        warnings.append(
            MigrationWarning(
                severity="warning",
                service=service.name,
                code="PROBE_PORT_UNKNOWN",
                message=(
                    f"Service '{service.name}' has an HTTP healthcheck but no known port; "
                    "the probe was rendered as an exec probe instead."
                ),
            )
        )

    readiness = ProbePlan(
        kind="exec",
        command=command,
        initial_delay_seconds=min(start_period, 10),
        period_seconds=period,
        timeout_seconds=timeout,
        failure_threshold=retries,
    )
    liveness = readiness.model_copy(
        update={"initial_delay_seconds": start_period, "period_seconds": max(period, 15)}
    )
    return readiness, liveness


_URL_RE = re.compile(r"https?://(?P<host>[^/:\s]+)(?::(?P<port>\d+))?(?P<path>/[^\s\"']*)?")


def _http_probe_from_command(command: Iterable[str]) -> tuple[str, int | None] | None:
    """Recognise `curl`/`wget` healthchecks so they become real httpGet probes."""
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
    host = match.group("host")
    # Only localhost URLs describe the container itself.
    if host not in {"localhost", "127.0.0.1", "0.0.0.0", "[::1]"}:
        return None
    port = int(match.group("port")) if match.group("port") else None
    return match.group("path") or "/", port


def _interpolate(value: str | None, defaults: dict[str, str]) -> tuple[str | None, list[str]]:
    """Resolve ``${VAR}`` / ``${VAR:-fallback}`` against known values."""
    if value is None:
        return None, []
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name in defaults:
            return defaults[name]
        fallback = match.group("default")
        if fallback is not None:
            return fallback
        missing.append(name)
        return match.group(0)

    return _INTERPOLATION_RE.sub(replace, value), missing


def _load_interpolation_values(root: Path) -> tuple[dict[str, str], str | None]:
    """Read .env (or .env.example as a fallback) for Compose interpolation.

    Secret values are dropped: they must never end up in a generated ConfigMap.
    """
    for candidate in (".env", ".env.example", ".env.sample"):
        path = root / candidate
        if not path.is_file():
            continue
        values = {
            key: value
            for key, value in docker_service._parse_env_file(path).items()
            if value is not None and not classify_env(key, value)
        }
        return values, candidate
    return {}, None


def _dockerfile_ports(root: Path, compose: ComposeInspection) -> dict[str, list[int]]:
    """Map Compose service name -> EXPOSE ports from its Dockerfile."""
    result: dict[str, list[int]] = {}
    for service in compose.services:
        if not service.build_context:
            continue
        context = (root / service.build_context).resolve()
        dockerfile = context / (service.dockerfile or "Dockerfile")
        if not dockerfile.is_file():
            continue
        try:
            info: DockerfileInspection = docker_service.inspect_dockerfile(str(dockerfile))
        except Exception:  # noqa: BLE001 - a broken Dockerfile must not stop planning
            logger.warning("Could not parse %s", dockerfile)
            continue
        result[service.name] = info.exposed_ports
    return result


def _summarise_resources(plans: list[ServicePlan], namespace: str) -> list[str]:
    resources = [f"Namespace/{namespace}"]
    for plan in plans:
        resources.append(f"{plan.workload_kind}/{plan.name}")
        if plan.create_service:
            resources.append(f"Service/{plan.name}")
        if plan.config_map_name:
            resources.append(f"ConfigMap/{plan.config_map_name}")
        if plan.secret_name:
            resources.append(f"Secret/{plan.secret_name}")
        for volume in plan.volumes:
            if volume.source_kind == "named" and plan.workload_kind == "Deployment":
                resources.append(f"PersistentVolumeClaim/{plan.name}-{volume.name}")
    return resources


def _summarise_ports(plans: list[ServicePlan]) -> list[str]:
    summary: list[str] = []
    for plan in plans:
        for port in plan.service_ports:
            entry = f"{plan.name}: {plan.service_type} {port.port} -> container {port.target_port}"
            if port.node_port:
                entry += f" (nodePort {port.node_port})"
            summary.append(entry)
    return summary


def _k8s_name(name: str) -> str:
    """Coerce a name into a DNS-1123 label."""
    slug = re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-")
    return (slug or "app")[:63].rstrip("-")


def _port_name(port: int) -> str:
    """Kubernetes port names are max 15 chars and must start with a letter."""
    known = {80: "http", 443: "https", 3306: "mysql", 5432: "postgres", 6379: "redis", 8000: "http", 8080: "http"}
    return known.get(port, f"port-{port}")

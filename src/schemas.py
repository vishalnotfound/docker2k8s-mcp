"""Pydantic models shared by tools, services, generators and validators.

These double as the MCP tools' structured-output schemas, so every MCP client
gets a typed contract without any client-specific code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["error", "warning", "info"]
WorkloadKind = Literal["Deployment", "StatefulSet"]


# --------------------------------------------------------------------------
# Docker inspection
# --------------------------------------------------------------------------
class ProjectInspection(BaseModel):
    """What a project directory contains."""

    project_path: str
    project_name: str
    dockerfile: bool = False
    dockerfile_paths: list[str] = Field(default_factory=list)
    compose_file: bool = False
    compose_path: str | None = None
    env_file: bool = False
    env_files: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    source_dirs: list[str] = Field(default_factory=list)
    dependency_files: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DockerfileStage(BaseModel):
    index: int
    base_image: str
    name: str | None = None


class DockerfileInspection(BaseModel):
    path: str
    stages: list[DockerfileStage] = Field(default_factory=list)
    final_base_image: str | None = None
    multi_stage: bool = False
    exposed_ports: list[int] = Field(default_factory=list)
    workdir: str | None = None
    user: str | None = None
    entrypoint: list[str] | None = None
    cmd: list[str] | None = None
    env_keys: list[str] = Field(default_factory=list)
    build_args: list[str] = Field(default_factory=list)
    healthcheck: str | None = None
    volumes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PortMapping(BaseModel):
    container_port: int
    host_port: int | None = None
    protocol: str = "tcp"


class VolumeMount(BaseModel):
    target: str
    source: str | None = None
    kind: Literal["named", "bind", "anonymous", "tmpfs"] = "named"
    read_only: bool = False


class HealthCheck(BaseModel):
    test: list[str] = Field(default_factory=list)
    interval_seconds: int | None = None
    timeout_seconds: int | None = None
    retries: int | None = None
    start_period_seconds: int | None = None
    disabled: bool = False


class ResourceLimits(BaseModel):
    cpu_limit: str | None = None
    memory_limit: str | None = None
    cpu_request: str | None = None
    memory_request: str | None = None


class ComposeService(BaseModel):
    """A single Compose service, normalised out of the short/long YAML syntaxes."""

    name: str
    image: str | None = None
    build_context: str | None = None
    dockerfile: str | None = None
    build_args: list[str] = Field(default_factory=list)
    command: list[str] | None = None
    entrypoint: list[str] | None = None
    ports: list[PortMapping] = Field(default_factory=list)
    expose: list[int] = Field(default_factory=list)
    # Values are redacted for anything that looks like a credential.
    environment: dict[str, str | None] = Field(default_factory=dict)
    secret_keys: list[str] = Field(default_factory=list)
    env_files: list[str] = Field(default_factory=list)
    volumes: list[VolumeMount] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    healthcheck: HealthCheck | None = None
    restart: str | None = None
    networks: list[str] = Field(default_factory=list)
    replicas: int | None = None
    resources: ResourceLimits | None = None
    user: str | None = None
    working_dir: str | None = None
    privileged: bool = False
    stateful: bool = False


class ComposeInspection(BaseModel):
    compose_path: str
    project_name: str
    services: list[ComposeService] = Field(default_factory=list)
    named_volumes: list[str] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EnvVarInfo(BaseModel):
    key: str
    is_secret: bool
    source: str
    has_value: bool = True
    value: str | None = None  # None whenever is_secret is True


class EnvironmentInspection(BaseModel):
    project_path: str
    files: list[str] = Field(default_factory=list)
    variables: list[EnvVarInfo] = Field(default_factory=list)
    secret_count: int = 0
    config_count: int = 0
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Analysis and migration plan
# --------------------------------------------------------------------------
class ConceptMapping(BaseModel):
    """One Docker concept and the Kubernetes concept it becomes."""

    service: str
    docker_concept: str
    kubernetes_concept: str
    detail: str


class MigrationWarning(BaseModel):
    """A migration concern. Severity 'error' means it blocks safe automation."""

    severity: Severity = "warning"
    service: str | None = None
    code: str
    message: str
    suggestion: str | None = None


class ProjectAnalysis(BaseModel):
    project_path: str
    project_name: str
    service_count: int
    services: list[str] = Field(default_factory=list)
    stateful_services: list[str] = Field(default_factory=list)
    externally_exposed: list[str] = Field(default_factory=list)
    mappings: list[ConceptMapping] = Field(default_factory=list)
    warnings: list[MigrationWarning] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    summary: str = ""


class ContainerPortPlan(BaseModel):
    name: str
    container_port: int
    protocol: str = "TCP"


class ServicePortPlan(BaseModel):
    name: str
    port: int
    target_port: int
    node_port: int | None = None
    protocol: str = "TCP"


class VolumePlan(BaseModel):
    name: str
    mount_path: str
    size: str = "1Gi"
    access_mode: str = "ReadWriteOnce"
    storage_class: str | None = None
    read_only: bool = False
    source_kind: Literal["named", "bind", "anonymous"] = "named"


class ProbePlan(BaseModel):
    kind: Literal["httpGet", "exec", "tcpSocket"]
    path: str | None = None
    port: int | None = None
    command: list[str] | None = None
    initial_delay_seconds: int = 10
    period_seconds: int = 10
    timeout_seconds: int = 5
    failure_threshold: int = 3


class EnvVarPlan(BaseModel):
    key: str
    value: str | None = None
    is_secret: bool = False
    source: Literal["configmap", "secret", "literal"] = "configmap"


class ServicePlan(BaseModel):
    """Everything needed to render one Compose service into Kubernetes."""

    name: str
    image: str
    build_required: bool = False
    build_context: str | None = None
    workload_kind: WorkloadKind = "Deployment"
    replicas: int = 1
    image_pull_policy: str = "IfNotPresent"
    command: list[str] | None = None
    args: list[str] | None = None
    container_ports: list[ContainerPortPlan] = Field(default_factory=list)
    create_service: bool = True
    service_type: Literal["ClusterIP", "NodePort", "LoadBalancer"] = "ClusterIP"
    headless: bool = False
    service_ports: list[ServicePortPlan] = Field(default_factory=list)
    config_map_name: str | None = None
    secret_name: str | None = None
    env: list[EnvVarPlan] = Field(default_factory=list)
    volumes: list[VolumePlan] = Field(default_factory=list)
    liveness_probe: ProbePlan | None = None
    readiness_probe: ProbePlan | None = None
    resources: ResourceLimits | None = None
    depends_on: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class MigrationPlan(BaseModel):
    """The reviewable artefact a user approves before anything is deployed."""

    project_path: str
    project_name: str
    namespace: str = "default"
    services: list[ServicePlan] = Field(default_factory=list)
    # Flat summaries so a client can render the plan without walking services.
    resources: list[str] = Field(default_factory=list)
    ports: list[str] = Field(default_factory=list)
    environment_variables: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)
    healthchecks: list[str] = Field(default_factory=list)
    warnings: list[MigrationWarning] = Field(default_factory=list)
    manual_steps: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Generation and validation
# --------------------------------------------------------------------------
class GeneratedManifest(BaseModel):
    path: str
    kind: str
    name: str


class GenerationResult(BaseModel):
    output_dir: str
    project_name: str
    namespace: str
    manifests: list[GeneratedManifest] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    next_step: str = "Call validate_manifests on output_dir before deploying."


class ValidationIssue(BaseModel):
    severity: Severity
    code: str
    message: str
    file: str | None = None
    resource: str | None = None
    suggestion: str | None = None


class ValidationResult(BaseModel):
    valid: bool
    path: str
    files_checked: int = 0
    resources: list[str] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    issues: list[ValidationIssue] = Field(default_factory=list)
    summary: str = ""


# --------------------------------------------------------------------------
# Kubernetes runtime
# --------------------------------------------------------------------------
class AppliedResource(BaseModel):
    kind: str
    name: str
    action: str  # created / configured / unchanged


class ApplyResult(BaseModel):
    applied: bool
    dry_run: bool
    namespace: str
    resources: list[AppliedResource] = Field(default_factory=list)
    output: str = ""
    next_step: str = ""


class ContainerState(BaseModel):
    name: str
    ready: bool = False
    restart_count: int = 0
    state: str = "unknown"
    reason: str | None = None
    message: str | None = None
    exit_code: int | None = None


class PodInfo(BaseModel):
    name: str
    phase: str
    ready: str = "0/0"
    restarts: int = 0
    node: str | None = None
    ip: str | None = None
    age_seconds: int | None = None
    containers: list[ContainerState] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class DeploymentStatus(BaseModel):
    name: str
    namespace: str
    kind: str = "Deployment"
    exists: bool = True
    desired_replicas: int = 0
    ready_replicas: int = 0
    available_replicas: int = 0
    updated_replicas: int = 0
    conditions: list[str] = Field(default_factory=list)
    selector: dict[str, str] = Field(default_factory=dict)
    healthy: bool = False


class ServiceInfo(BaseModel):
    name: str
    namespace: str
    type: str
    cluster_ip: str | None = None
    ports: list[str] = Field(default_factory=list)
    selector: dict[str, str] = Field(default_factory=dict)
    endpoint_count: int = 0
    has_endpoints: bool = False


class EventInfo(BaseModel):
    type: str
    reason: str
    object: str
    message: str
    count: int = 1
    last_seen: str | None = None


class VerificationCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class VerificationResult(BaseModel):
    healthy: bool
    namespace: str
    checks: list[VerificationCheck] = Field(default_factory=list)
    deployments: list[DeploymentStatus] = Field(default_factory=list)
    pods: list[PodInfo] = Field(default_factory=list)
    services: list[ServiceInfo] = Field(default_factory=list)
    summary: str = ""
    next_step: str = ""


class Finding(BaseModel):
    problem: str
    resource: str
    evidence: str
    likely_cause: str
    suggested_fix: str


class Diagnosis(BaseModel):
    namespace: str
    healthy: bool
    findings: list[Finding] = Field(default_factory=list)
    recent_events: list[EventInfo] = Field(default_factory=list)
    summary: str = ""

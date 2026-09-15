"""Cluster tools: deploy, observe, verify and diagnose.

Every operation here is a named, controlled action.  There is deliberately no
tool that runs an arbitrary kubectl or shell command.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..errors import ApprovalRequired
from ..schemas import (
    ApplyResult,
    DeploymentStatus,
    Diagnosis,
    EventInfo,
    PodInfo,
    ServiceInfo,
    ValidationResult,
    VerificationResult,
)
from ..services import k8s_service
from ..validators import kubernetes as validator
from . import mcp_errors

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)


def register(mcp: MCPServer) -> None:
    """Attach the Kubernetes deployment and observability tools to the server."""

    @mcp.tool(
        title="Check cluster connection",
        annotations=READ_ONLY,
        description=(
            "Confirm a Kubernetes cluster is reachable and list its nodes and namespaces. "
            "Call this before deploying so a connection problem is not mistaken for a "
            "deployment failure."
        ),
    )
    @mcp_errors
    async def get_cluster_info() -> dict:
        """Report cluster reachability, nodes and namespaces."""
        return await k8s_service.cluster_info()

    @mcp.tool(
        title="Apply manifests to Kubernetes",
        annotations=DESTRUCTIVE,
        description=(
            "DEPLOY. This writes to the cluster and changes running workloads. "
            "Requires approved=true, which you may only pass after the user has seen the "
            "migration plan and its warnings and has explicitly said to deploy. Do not "
            "infer approval from the original request. Run validate_manifests first; this "
            "tool refuses to apply manifests that fail validation. Use dry_run=true to "
            "have the API server check the manifests without changing anything -- a dry "
            "run needs no approval."
        ),
    )
    @mcp_errors
    async def apply_manifests(
        path: str,
        namespace: str | None = None,
        approved: bool = False,
        dry_run: bool = False,
    ) -> ApplyResult:
        """Apply generated manifests to the cluster.

        Args:
            path: Manifest file or directory (the output_dir from generate_manifests).
            namespace: Target namespace. Defaults to the server's configured one.
            approved: Must be true for a real deployment. Set it only after the user
                has explicitly approved this deployment.
            dry_run: Server-side dry run. Validates against the live API without
                changing anything.
        """
        if not dry_run and not approved:
            raise ApprovalRequired(
                "Deployment was not approved.",
                hint=(
                    "Show the user the migration plan, the validation result and the "
                    "warnings, ask whether to deploy, and call this tool again with "
                    "approved=true only if they say yes. Use dry_run=true to check the "
                    "manifests against the cluster without deploying."
                ),
            )

        result: ValidationResult = validator.validate_manifests(path)
        if not result.valid:
            errors = "\n".join(
                f"- [{i.code}] {i.message}" for i in result.issues if i.severity == "error"
            )
            raise ToolError(
                "ValidationError: refusing to apply manifests that fail validation.\n"
                f"{errors}\n"
                "What to try: fix the manifests (or regenerate them) and validate again."
            )

        return await k8s_service.apply_manifests(path, namespace=namespace, dry_run=dry_run)

    @mcp.tool(
        title="Get deployment status",
        annotations=READ_ONLY,
        description=(
            "Report desired vs ready replicas and the rollout conditions for a Deployment, "
            "falling back to a StatefulSet of the same name."
        ),
    )
    @mcp_errors
    async def get_deployment_status(name: str, namespace: str | None = None) -> DeploymentStatus:
        """Get replica counts and conditions for one workload.

        Args:
            name: Deployment or StatefulSet name.
            namespace: Namespace to look in.
        """
        return await k8s_service.get_deployment_status(name, namespace)

    @mcp.tool(
        title="List pods",
        annotations=READ_ONLY,
        description=(
            "List pods with phase, ready count, restart count and any detected problem "
            "(CrashLoopBackOff, ImagePullBackOff, Pending, OOMKilled, "
            "CreateContainerConfigError). Use this first when a deployment looks unhealthy."
        ),
    )
    @mcp_errors
    async def get_pods(namespace: str | None = None, selector: str | None = None) -> list[PodInfo]:
        """List pods in a namespace.

        Args:
            namespace: Namespace to list. Defaults to the server's configured one.
            selector: Optional label selector, e.g. "app.kubernetes.io/name=api".
        """
        return await k8s_service.get_pods(namespace, selector)

    @mcp.tool(
        title="Get pod logs",
        annotations=READ_ONLY,
        description=(
            "Read a pod's container logs. For a crash-looping pod set previous=true to "
            "read the instance that already died -- that is where the real error is."
        ),
    )
    @mcp_errors
    async def get_pod_logs(
        pod_name: str,
        namespace: str | None = None,
        container: str | None = None,
        tail_lines: int = 100,
        previous: bool = False,
    ) -> str:
        """Read logs from one pod.

        Args:
            pod_name: Exact pod name, from get_pods.
            namespace: Namespace the pod runs in.
            container: Container name, when the pod has more than one.
            tail_lines: How many trailing lines to return (max 2000).
            previous: Read the previous, terminated container instance.
        """
        return await k8s_service.get_pod_logs(
            pod_name, namespace, container, tail_lines, previous
        )

    @mcp.tool(
        title="List services",
        annotations=READ_ONLY,
        description=(
            "List Services with their type, ports, selector and endpoint count. A Service "
            "with zero endpoints is the usual reason an application is unreachable even "
            "though its pods look fine."
        ),
    )
    @mcp_errors
    async def get_services(
        namespace: str | None = None, name: str | None = None
    ) -> list[ServiceInfo]:
        """List Services and their endpoints.

        Args:
            namespace: Namespace to list.
            name: Return only the Service with this name.
        """
        return await k8s_service.get_services(namespace, name)

    @mcp.tool(
        title="Get namespace events",
        annotations=READ_ONLY,
        description=(
            "Return recent Kubernetes events, oldest first. Events explain scheduling "
            "failures, image pull errors, probe failures and volume mount problems that "
            "the pod status alone does not."
        ),
    )
    @mcp_errors
    async def get_events(namespace: str | None = None, limit: int = 30) -> list[EventInfo]:
        """Read recent events in a namespace.

        Args:
            namespace: Namespace to read.
            limit: Maximum number of events to return (max 200).
        """
        return await k8s_service.get_events(namespace, limit)

    @mcp.tool(
        title="Verify deployment",
        annotations=READ_ONLY,
        description=(
            "Run the post-deployment checks in one call: the workload exists, ready "
            "replicas match desired, pods are Running and Ready, restart counts are zero, "
            "Services exist and have endpoints, and optionally that a health endpoint "
            "responds through the API server proxy. Never assume a deployment succeeded -- "
            "call this. If healthy is false, call diagnose_deployment."
        ),
    )
    @mcp_errors
    async def verify_deployment(
        namespace: str | None = None,
        names: list[str] | None = None,
        health_path: str | None = None,
    ) -> VerificationResult:
        """Verify that a deployment is actually serving.

        Args:
            namespace: Namespace to verify.
            names: Workload names to check. Defaults to everything this tool deployed.
            health_path: Optional HTTP path to probe, e.g. "/health".
        """
        return await k8s_service.verify_deployment(namespace, names, health_path)

    @mcp.tool(
        title="Diagnose deployment failure",
        annotations=READ_ONLY,
        description=(
            "Gather pods, container states, Services and warning events, and return a "
            "diagnosis: what is wrong, the evidence, the likely cause and a suggested fix. "
            "Recognises CrashLoopBackOff, ImagePullBackOff, Pending, OOMKilled, "
            "CreateContainerConfigError, failing probes, Services without endpoints and "
            "PVC problems. Call this whenever verify_deployment reports healthy=false."
        ),
    )
    @mcp_errors
    async def diagnose_deployment(
        namespace: str | None = None, names: list[str] | None = None
    ) -> Diagnosis:
        """Diagnose why a deployment is unhealthy.

        Args:
            namespace: Namespace to diagnose.
            names: Workload names to focus on. Defaults to everything this tool deployed.
        """
        return await k8s_service.diagnose_deployment(namespace, names)

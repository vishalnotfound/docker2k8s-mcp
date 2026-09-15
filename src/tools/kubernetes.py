"""Manifest generation and validation tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..generators import kubernetes as generator
from ..schemas import GenerationResult, ValidationResult
from ..services import migration_service
from . import mcp_errors

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
WRITES_FILES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)


def register(mcp: MCPServer) -> None:
    """Attach the generation and validation tools to the server."""

    @mcp.tool(
        title="Generate Kubernetes manifests",
        annotations=WRITES_FILES,
        description=(
            "Write Kubernetes YAML for the project into a per-project directory under the "
            "server's generated/ folder. Only the resources the application actually needs "
            "are produced: a stateless service gets a Deployment, Service and ConfigMap, "
            "while a database also gets a StatefulSet with volumeClaimTemplates and a "
            "headless Service. Secrets are written with <REQUIRED_SECRET> placeholders, "
            "never real credentials. The plan is re-derived from the project, so pass the "
            "same namespace/image_registry you passed to create_migration_plan. Writes "
            "files only -- it does not touch a cluster."
        ),
    )
    @mcp_errors
    def generate_manifests(
        path: str,
        namespace: str | None = None,
        image_registry: str | None = None,
        output_dir: str | None = None,
        include_ingress: bool = False,
        ingress_host: str | None = None,
    ) -> GenerationResult:
        """Generate Kubernetes manifests for a Docker project.

        Args:
            path: Project directory containing a Compose file.
            namespace: Target namespace. Defaults to the server's configured one.
            image_registry: Registry prefix for locally built images.
            output_dir: Override the output directory. Defaults to
                generated/<project-name>/k8s, which keeps projects from overwriting
                each other.
            include_ingress: Also generate an Ingress for externally exposed services.
                Requires an ingress controller in the cluster.
            ingress_host: Hostname for the generated Ingress rules.
        """
        plan = migration_service.create_migration_plan(
            path, namespace=namespace, image_registry=image_registry
        )
        return generator.generate_manifests(
            plan,
            output_dir=output_dir,
            include_ingress=include_ingress,
            ingress_host=ingress_host,
        )

    @mcp.tool(
        title="Validate Kubernetes manifests",
        annotations=READ_ONLY,
        description=(
            "Check manifests for the mistakes that survive 'kubectl apply' but break the "
            "application: selectors that match no pods, a Service targetPort no container "
            "listens on, probes aimed at the wrong port, references to a ConfigMap, Secret "
            "or PVC that does not exist, undefined volume mounts, duplicate resources, "
            "wrong apiVersion, invalid names and out-of-range nodePorts. ALWAYS run this "
            "before deploying. Returns valid=false with human-readable errors when the "
            "manifests would break."
        ),
    )
    @mcp_errors
    def validate_manifests(path: str) -> ValidationResult:
        """Validate a manifest file or a directory of manifests.

        Args:
            path: A YAML file, or the output_dir returned by generate_manifests.
        """
        from ..validators import kubernetes as validator

        return validator.validate_manifests(path)

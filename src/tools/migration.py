"""Migration analysis and planning tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..schemas import MigrationPlan, ProjectAnalysis
from ..services import migration_service
from . import mcp_errors

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(mcp: MCPServer) -> None:
    """Attach the analysis and planning tools to the server."""

    @mcp.tool(
        title="Analyze project for migration",
        annotations=READ_ONLY,
        description=(
            "Explain how this project's Docker concepts map to Kubernetes: containers to "
            "Deployments or StatefulSets, port mappings to Services, environment to "
            "ConfigMaps and Secrets, volumes to PersistentVolumeClaims, HEALTHCHECK to "
            "readiness and liveness probes, and Compose service names to Service DNS. "
            "Also reports what cannot be migrated automatically (bind mounts, depends_on "
            "ordering, locally built images) as warnings and blockers."
        ),
    )
    @mcp_errors
    def analyze_project(path: str) -> ProjectAnalysis:
        """Analyze a Docker project and map its concepts onto Kubernetes.

        Args:
            path: Project directory containing a Compose file.
        """
        return migration_service.analyze_project(path)

    @mcp.tool(
        title="Create migration plan",
        annotations=READ_ONLY,
        description=(
            "Build the concrete, reviewable migration plan: per-service workload kind, "
            "replicas, images, ports, ConfigMap/Secret split, volumes, probes and "
            "resources, plus flat summaries of resources, ports, environment variables, "
            "secrets, volumes and healthchecks, with warnings and manual_steps. "
            "SHOW THIS PLAN TO THE USER before generating or deploying anything. Nothing "
            "is written to disk and no cluster is touched."
        ),
    )
    @mcp_errors
    def create_migration_plan(
        path: str,
        namespace: str | None = None,
        image_registry: str | None = None,
    ) -> MigrationPlan:
        """Produce the migration plan for a project.

        Args:
            path: Project directory containing a Compose file.
            namespace: Target Kubernetes namespace. Defaults to the server's configured one.
            image_registry: Registry prefix to prepend to locally built images,
                e.g. "ghcr.io/acme". Leave unset for a local Docker Desktop cluster.
        """
        return migration_service.create_migration_plan(
            path, namespace=namespace, image_registry=image_registry
        )

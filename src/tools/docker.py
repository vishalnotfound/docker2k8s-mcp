"""Docker inspection tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..schemas import (
    ComposeInspection,
    DockerfileInspection,
    EnvironmentInspection,
    ProjectInspection,
)
from ..services import docker_service
from . import mcp_errors

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(mcp: MCPServer) -> None:
    """Attach the Docker inspection tools to the server."""

    @mcp.tool(
        title="Inspect Docker project",
        annotations=READ_ONLY,
        description=(
            "Start here. Given a project directory, report which Docker artefacts it "
            "contains: Dockerfile(s), a Compose file, .env files, source directories, "
            "dependency files and the names of the Compose services. Secret values are "
            "never returned."
        ),
    )
    @mcp_errors
    def inspect_project(path: str) -> ProjectInspection:
        """Identify the Docker-relevant files in a project directory.

        Args:
            path: Directory containing the Dockerfile and/or docker-compose.yml.
        """
        return docker_service.inspect_project(path)

    @mcp.tool(
        title="Inspect Dockerfile",
        annotations=READ_ONLY,
        description=(
            "Parse a Dockerfile into the facts a migration needs: base images and build "
            "stages, EXPOSE ports, WORKDIR, USER, ENTRYPOINT/CMD, declared ENV keys and "
            "the HEALTHCHECK. Use it to learn which port an image actually listens on."
        ),
    )
    @mcp_errors
    def inspect_dockerfile(path: str) -> DockerfileInspection:
        """Parse a Dockerfile.

        Args:
            path: Path to a Dockerfile, or a directory containing one.
        """
        return docker_service.inspect_dockerfile(path)

    @mcp.tool(
        title="Inspect Docker Compose",
        annotations=READ_ONLY,
        description=(
            "Parse a Compose file into normalised services: images, build contexts, port "
            "mappings, environment, env_file, volumes, depends_on, healthchecks, restart "
            "policy, networks and resource limits. Values that look like credentials are "
            "returned as <REDACTED> and listed by key in secret_keys."
        ),
    )
    @mcp_errors
    def inspect_compose(path: str) -> ComposeInspection:
        """Parse a docker-compose.yml.

        Args:
            path: Path to a Compose file, or a directory containing one.
        """
        return docker_service.inspect_compose(path)

    @mcp.tool(
        title="Inspect environment variables",
        annotations=READ_ONLY,
        description=(
            "List the environment variables the project uses, from .env files and from "
            "Compose, and classify each as configuration or secret. Secret VALUES are "
            "never returned -- only the key names -- so this is safe to call on a project "
            "with real credentials."
        ),
    )
    @mcp_errors
    def inspect_environment(path: str) -> EnvironmentInspection:
        """List environment variables and classify them.

        Args:
            path: Project directory to inspect.
        """
        return docker_service.inspect_environment(path)

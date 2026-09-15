"""MCP server setup and tool registration.

The server is transport-agnostic: ``build_server()`` returns a configured
MCPServer that ``main.py`` runs over stdio, streamable HTTP or SSE.  Nothing in
here is specific to any one MCP client.
"""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer

from .config import get_settings
from .tools import deployment, docker, kubernetes, migration

logger = logging.getLogger(__name__)

SERVER_NAME = "docker2k8s"
SERVER_VERSION = "0.1.0"

#: Sent to the client on initialize. Clients surface this to their model, so it
#: is how a generic MCP client learns the workflow without any custom code.
INSTRUCTIONS = """\
docker2k8s migrates a Docker / Docker Compose application to Kubernetes.

Work through these phases, choosing tools based on what you have learned so far
rather than following a fixed script:

1. INSPECT   inspect_project, then inspect_compose and inspect_dockerfile for
             detail, and inspect_environment to see which variables are secrets.
2. ANALYZE   analyze_project explains how each Docker concept maps to Kubernetes
             and what cannot be translated automatically.
3. PLAN      create_migration_plan produces the reviewable plan. Show the user
             its warnings and manual_steps.
4. GENERATE  generate_manifests writes YAML under generated/<project>/k8s.
5. VALIDATE  validate_manifests. Never skip this. If valid=false, fix the cause
             and regenerate; do not deploy.
6. APPROVE   Present the plan, the validation result and the warnings, then ask
             the user whether to deploy. Wait for an explicit yes.
7. DEPLOY    apply_manifests with approved=true. Use dry_run=true beforehand to
             check against the live API without changing anything.
8. VERIFY    verify_deployment. Never assume the deployment worked.
9. DIAGNOSE  If verification fails, diagnose_deployment, then get_pod_logs
             (previous=true for a crash loop) and get_events for detail. Explain
             the cause and the fix, then iterate.

Rules:
- Never deploy without explicit user approval in this conversation.
- Never skip validation.
- Secret values are never returned by these tools. Generated Secrets contain
  <REQUIRED_SECRET> placeholders that a human must fill in before the app works.
- Locally built images must exist in a registry the cluster can reach. Docker
  Desktop shares its image store with its Kubernetes cluster; other clusters do not.
"""


def build_server() -> MCPServer:
    """Create the MCP server with every tool registered."""
    settings = get_settings()

    mcp = MCPServer(
        name=SERVER_NAME,
        title="Docker to Kubernetes Migration",
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        log_level=settings.log_level,  # type: ignore[arg-type]
        debug=settings.debug,
    )

    docker.register(mcp)
    migration.register(mcp)
    kubernetes.register(mcp)
    deployment.register(mcp)
    _register_prompts(mcp)

    logger.info(
        "MCP server '%s' ready (generated_dir=%s, namespace=%s)",
        SERVER_NAME,
        settings.generated_dir,
        settings.kube_namespace,
    )
    return mcp


def _register_prompts(mcp: MCPServer) -> None:
    """Expose the workflow as MCP prompts.

    Clients that support prompts (Claude Desktop's slash commands, for instance)
    can start a migration without the user having to describe the process.
    """

    @mcp.prompt(
        name="migrate",
        title="Migrate a Docker project to Kubernetes",
        description="Run the full inspect - analyze - plan - generate - validate - deploy workflow.",
    )
    def migrate(project_path: str, namespace: str = "default") -> str:
        return (
            f"Migrate the Docker application at '{project_path}' to Kubernetes in the "
            f"'{namespace}' namespace.\n\n"
            "Inspect the project, analyze it, create a migration plan, generate the "
            "manifests and validate them. Then show me the plan, the warnings and the "
            "validation result, and ask before deploying anything. After I approve, "
            "deploy, verify the result, and diagnose any failure."
        )

    @mcp.prompt(
        name="diagnose",
        title="Diagnose a failed Kubernetes deployment",
        description="Investigate why workloads in a namespace are unhealthy.",
    )
    def diagnose(namespace: str = "default") -> str:
        return (
            f"The deployment in namespace '{namespace}' is not healthy. Diagnose it: "
            "check the workloads, pods, services and events, read the logs of any failing "
            "container (including the previous instance if it is crash-looping), and tell "
            "me the cause and how to fix it. Do not change anything without asking."
        )

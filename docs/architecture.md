# Architecture

## Overview

docker2k8s is an MCP server. It exposes a set of narrow, controlled tools that
let any MCP-capable LLM client inspect a Docker project, reason about it, and
migrate it to Kubernetes.

The server holds no conversation state and calls no LLM itself. The *agent loop
lives in the client*, which is what makes the server usable from Claude Desktop,
Claude Code, Cursor, VS Code, a custom script, or the bundled reference agent.

```text
┌──────────────────────────────────────────────────────────┐
│  MCP Client + LLM                                        │
│  Claude Desktop / Claude Code / Cursor / client/agent.py │
└───────────────────────────┬──────────────────────────────┘
                            │ MCP (stdio | streamable HTTP | SSE)
                            ▼
┌──────────────────────────────────────────────────────────┐
│  MCP Server            src/server.py, src/main.py        │
│  name: docker2k8s   17 tools, 2 prompts, instructions    │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│  Tools                 src/tools/*.py                    │
│  Thin wrappers: no business logic                        │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│  Services / Generators / Validators                      │
│                                                          │
│  docker_service     ──▶ Filesystem + YAML                │
│  migration_service  ──▶ (pure logic)                     │
│  generators/…       ──▶ Filesystem (generated/)          │
│  validators/…       ──▶ (pure logic)                     │
│  k8s_service        ──▶ Kubernetes API + kubectl         │
└──────────────────────────────────────────────────────────┘
```

## Layers

| Layer | Location | Responsibility |
|---|---|---|
| Entry point | `src/main.py` | CLI, transport selection, logging |
| Server | `src/server.py` | MCP server, tool registration, workflow instructions |
| Tools | `src/tools/` | MCP surface: schemas, descriptions, error translation |
| Services | `src/services/` | All business logic and I/O |
| Generators | `src/generators/` | Plan → Kubernetes YAML |
| Validators | `src/validators/` | Manifests → issues |
| Schemas | `src/schemas.py` | Pydantic models, shared by every layer |
| Config | `src/config.py` | Environment-driven settings |
| Security | `src/security.py` | Path sandbox, secret detection and redaction |
| Errors | `src/errors.py` | Domain errors carrying a hint for the model |

A tool never contains logic:

```python
@mcp.tool(...)
@mcp_errors
def inspect_compose(path: str) -> ComposeInspection:
    return docker_service.inspect_compose(path)
```

## The migration pipeline

```text
inspect_project ──▶ inspect_compose ──▶ analyze_project ──▶ create_migration_plan
                    inspect_dockerfile                              │
                    inspect_environment                             ▼
                                                          generate_manifests
                                                                    │
                                                                    ▼
                                                          validate_manifests
                                                                    │
                                                     ┌──────────────┴───────────┐
                                                 valid=false               valid=true
                                                     │                          │
                                                  fix/regen          ask user for approval
                                                                                │
                                                                       apply_manifests
                                                                                │
                                                                       verify_deployment
                                                                                │
                                                                  ┌─────────────┴──────────┐
                                                              healthy                 unhealthy
                                                                 │                         │
                                                               done          diagnose_deployment
                                                                                           │
                                                                          get_pod_logs / get_events
```

The LLM chooses each step. The order above is what the server's `instructions`
recommend, not a state machine the server enforces — except for two hard gates:

- `apply_manifests` refuses without `approved=true`.
- `apply_manifests` refuses manifests that fail validation.

## Data model

`MigrationPlan` is the pivot of the whole system. Everything before it produces
it; everything after it consumes it.

```text
ComposeInspection          (what Docker says)
        │
        ▼
   ProjectAnalysis         (what it means for Kubernetes)
        │
        ▼
   MigrationPlan           (what we will do — the reviewable artefact)
        │
        ├──▶ GenerationResult   (YAML on disk)
        │            │
        │            ▼
        └──▶ ValidationResult   (is it correct?)
                     │
                     ▼
              ApplyResult → VerificationResult → Diagnosis
```

## Design decisions

**Reads use the Kubernetes Python client; writes use kubectl.** The client gives
typed objects for pods, services and events. `kubectl apply` handles
multi-document files, three-way merges and server-side dry runs correctly, and
reimplementing that would be a mistake. Every subprocess call is confined to
`k8s_service.py` and built as an explicit argv list.

**Secrets are redacted at the parsing boundary.** `docker_service` replaces
credential-looking values with `<REDACTED>` the moment it reads them, so no
downstream layer can leak one. Generated Kubernetes Secrets contain
`<REQUIRED_SECRET>` placeholders — a real credential never reaches a manifest.

**The filesystem is sandboxed.** `resolve_project_path` resolves `..` and then
asserts containment inside `ALLOWED_ROOTS`. The MCP server cannot read arbitrary
files.

**No generic escape hatch.** There is no `execute_shell` or `execute_kubectl`
tool. Each cluster capability is a separate, named, schema-checked tool.

**Generation is deterministic.** `generate_manifests` re-derives the plan from
the project rather than accepting a plan object from the model, so the YAML on
disk always corresponds to the real project rather than to something the model
may have paraphrased.

**Only necessary resources are generated.** A stateless API produces a
Deployment, a Service and a ConfigMap. A database additionally produces a
StatefulSet with `volumeClaimTemplates` and a headless Service. Nothing emits an
Ingress or a Namespace unless it is actually needed.

## Concept mapping

| Docker / Compose | Kubernetes | Notes |
|---|---|---|
| Container | Deployment | Stateless services |
| Container with a named volume, or a known datastore image | StatefulSet | Stable identity and storage |
| `ports: "8080:80"` | Service | Host publishing does not exist; NodePort or port-forward |
| Environment variables | ConfigMap | Non-sensitive values |
| Credential-looking variables | Secret | Written as placeholders |
| Named volume | PersistentVolumeClaim / volumeClaimTemplate | |
| Bind mount | *(no equivalent)* | Warning + manual step |
| `HEALTHCHECK` | readinessProbe + livenessProbe | Kubernetes separates ready from alive |
| Service name on a network | Service DNS | Compose names keep working |
| `depends_on` | *(no equivalent)* | Warning: pods start in parallel |
| `deploy.replicas` | `spec.replicas` | Forced to 1 for datastores |
| `deploy.resources` | `resources.limits/requests` | `512M` → `512Mi` |
| `restart: unless-stopped` | `restartPolicy: Always` | Deployments are always Always |

## Transports

`build_server()` returns a transport-agnostic `MCPServer`. `main.py` runs it over:

- **stdio** — the default; the client launches the server as a subprocess.
- **streamable HTTP** — `--transport http`, for remote or containerised servers.
- **SSE** — `--transport sse`, for older HTTP clients.

## Future work

See the roadmap in the README. Nothing in this design assumes a local cluster:
adding EKS means adding a credential provider to `k8s_service`, not restructuring
the pipeline.

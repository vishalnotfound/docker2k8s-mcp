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
│  Claude Desktop / Claude Code / Cursor / bundled agent   │
└───────────────────────────┬──────────────────────────────┘
                            │ MCP (stdio | streamable HTTP | SSE)
                            ▼
┌──────────────────────────────────────────────────────────┐
│  MCP Server            docker2k8s_mcp/server.py, main.py │
│  name: docker2k8s   17 tools, 2 prompts, instructions    │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│  Tools                 docker2k8s_mcp/tools/*.py         │
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
| Entry point | `docker2k8s_mcp/main.py` | CLI, transport selection, logging |
| HTTP auth | `docker2k8s_mcp/http_auth.py` | Bearer-token check for the http/sse transports |
| Server | `docker2k8s_mcp/server.py` | MCP server, tool registration, workflow instructions |
| Tools | `docker2k8s_mcp/tools/` | MCP surface: schemas, descriptions, error translation |
| Services | `docker2k8s_mcp/services/` | All business logic and I/O |
| Generators | `docker2k8s_mcp/generators/` | Plan → Kubernetes YAML |
| Validators | `docker2k8s_mcp/validators/` | Manifests → issues |
| Schemas | `docker2k8s_mcp/schemas.py` | Pydantic models, shared by every layer |
| Config | `docker2k8s_mcp/config.py` | Environment-driven settings |
| Security | `docker2k8s_mcp/security.py` | Path sandbox, secret detection and redaction |
| Errors | `docker2k8s_mcp/errors.py` | Domain errors carrying a hint for the model |
| Clients | `docker2k8s_mcp/client/` | Reference agent and helpers shared with the web client |

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
- `apply_manifests` refuses manifests that fail validation, and applies exactly
  the documents it validated.
- `apply_manifests` refuses Secrets that still hold placeholders (an existing
  Secret is left untouched) and objects another project owns.

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
three-way merges and server-side dry runs correctly, and reimplementing that
would be a mistake. The validated documents are streamed to one
`kubectl apply -f -`, namespaces first. Every subprocess call is confined to
`k8s_service.py` and built as an explicit argv list.

**Compose is read as Compose reads it.** The file is parsed with YAML 1.2
scalars (`restart: no` is a string, `22:22` is not base 60), and `${VAR}`
interpolation from `.env` applies to every field, not just `environment` —
except that a secret variable is never substituted anywhere.

**Secrets are redacted at the parsing boundary.** `docker_service` replaces
credential-looking values with `<REDACTED>` the moment it reads them, and drops
secret `.env` values as it streams the file, so no downstream layer can leak one.
Generated Kubernetes Secrets contain `<REQUIRED_SECRET>` placeholders — a real
credential never reaches a manifest.

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

**Readiness gates traffic; liveness only checks the process.** An HTTP
healthcheck becomes the readiness probe, while liveness is a TCP check on the
same port, so a database outage does not restart every API pod.

**Bounded memory.** Pod logs are capped at 256 KiB, the health probe reads 200
bytes, events keep only the requested number in memory, verification lists pods
and EndpointSlices once per call, and the clients truncate tool output kept in
the model's history.

## Concept mapping

| Docker / Compose | Kubernetes | Notes |
|---|---|---|
| Container | Deployment | Stateless services |
| Known datastore image (postgres, mysql, redis, …) | StatefulSet | Stable identity and per-pod storage |
| Other container with a named volume | Deployment + PVC | `Recreate` strategy; a volume shared by services becomes one shared PVC |
| `tmpfs` | emptyDir (`medium: Memory`) | |
| `ports: "8080:80"` | Service | Host publishing does not exist; NodePort or port-forward |
| Environment variables | ConfigMap | Non-sensitive values |
| Credential-looking variables | Secret | Written as placeholders |
| Named volume | PersistentVolumeClaim / volumeClaimTemplate | |
| Bind mount | *(no equivalent)* | Warning + manual step |
| `HEALTHCHECK` | readinessProbe + livenessProbe | Liveness is a TCP check, so dependencies cannot trigger restarts |
| *(no healthcheck)* | TCP readinessProbe | On the first TCP port |
| `privileged`, `cap_add`, `user` | securityContext | Privileged is also a blocker |
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

Both HTTP transports require `MCP_AUTH_TOKEN` to listen on anything but loopback.

## Future work

See the roadmap in the README. Nothing in this design assumes a local cluster:
adding EKS means adding a credential provider to `k8s_service`, not restructuring
the pipeline.

# docker2k8s-mcp

An MCP server that lets an LLM migrate a Docker / Docker Compose application to
Kubernetes — inspect it, plan the migration, generate manifests, validate them,
deploy them after you approve, verify the result, and diagnose what went wrong.

The interesting part is not the YAML. It is the loop:

```text
LLM ─▶ picks a tool ─▶ gets information ─▶ reasons ─▶ picks the next tool
  ─▶ generates configuration ─▶ validates ─▶ observes the deployment
  ─▶ diagnoses failures ─▶ iterates
```

**Works with any MCP client.** The server holds no agent logic and calls no LLM.
Use it from Claude Desktop, Claude Code, Cursor, VS Code, your own script, or the
reference agent included here.

---

## Contents

- [Architecture](#architecture)
- [Features](#features)
- [Installation](#installation)
- [Environment variables](#environment-variables)
- [Running the MCP server](#running-the-mcp-server)
- [Connecting a client](#connecting-a-client)
- [Running the reference client](#running-the-reference-client)
- [Requirements](#requirements)
- [Example migration](#example-migration)
- [Available MCP tools](#available-mcp-tools)
- [Security considerations](#security-considerations)
- [Development](#development)
- [Testing](#testing)
- [Roadmap](#roadmap)

---

## Architecture

```text
┌──────────────────────────────────────────────────────────┐
│  MCP Client + LLM                                        │
│  Claude Desktop / Claude Code / Cursor / client/agent.py │
└───────────────────────────┬──────────────────────────────┘
                            │ MCP (stdio | streamable HTTP | SSE)
                            ▼
┌──────────────────────────────────────────────────────────┐
│  MCP Server               src/server.py                  │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│  Tools (thin wrappers)    src/tools/                     │
└───────────────────────────┬──────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────┐
│  Services                 src/services/                  │
│    docker_service     ─▶ Filesystem + YAML               │
│    migration_service  ─▶ pure logic                      │
│    k8s_service        ─▶ Kubernetes API + kubectl        │
│  Generators / Validators                                 │
└──────────────────────────────────────────────────────────┘
```

The migration pipeline:

```text
inspect ─▶ analyze ─▶ plan ─▶ generate ─▶ validate ─▶ APPROVAL ─▶ deploy
                                                                    │
                                            verify ◀────────────────┘
                                              │
                                     healthy? ─┴─ no ─▶ diagnose ─▶ iterate
```

Full detail: [docs/architecture.md](docs/architecture.md).

---

## Features

- **Understands a project, rather than transliterating YAML.** Parses Compose's
  short and long syntaxes, Dockerfiles (multi-stage, line continuations, exec and
  shell forms), and `.env` files.
- **Knows what does not translate.** Bind mounts, `depends_on` ordering, locally
  built images, published database ports, privileged containers — each becomes a
  warning with a suggested action, not a silent omission.
- **Chooses the right workload.** Datastores and services with named volumes
  become StatefulSets with `volumeClaimTemplates` and a headless Service.
  Everything else becomes a Deployment.
- **Real probes from `HEALTHCHECK`.** A `curl` healthcheck against localhost
  becomes an `httpGet` probe; anything else becomes an `exec` probe. Readiness and
  liveness get different timings, because they mean different things.
- **Never emits a credential.** Secret values are redacted at the parsing
  boundary; generated Secrets contain `<REQUIRED_SECRET>` placeholders.
- **Validation that catches the silent failures.** Selectors that match nothing,
  a `targetPort` no container listens on, probes on the wrong port, references to
  a ConfigMap that does not exist.
- **Approval gate.** Deployment is refused without explicit approval, and refused
  outright if validation failed.
- **Diagnosis.** CrashLoopBackOff, ImagePullBackOff, Pending, OOMKilled,
  CreateContainerConfigError, failing probes, Services without endpoints — each
  with evidence, likely cause and a suggested fix.

---

## Installation

Requires Python 3.12+.

```bash
git clone <your-repo> docker2k8s-mcp
cd docker2k8s-mcp

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .

# The reference LLM client and the test suite are optional extras:
pip install -e ".[client,dev]"

cp .env.example .env
```

---

## Environment variables

Everything is configured through the environment or `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | *(empty)* | Reference client only. The server never calls an LLM. |
| `OPENAI_MODEL` | `gpt-4o` | Model used by the reference client. |
| `OPENAI_BASE_URL` | *(unset)* | For OpenAI-compatible endpoints. |
| `KUBECTL_PATH` | `kubectl` | Path to the kubectl binary. |
| `KUBE_CONTEXT` | *(current)* | kubectl context to use. |
| `KUBE_NAMESPACE` | `default` | Default target namespace. |
| `GENERATED_DIR` | `./generated` | Where manifests are written. |
| `ALLOWED_ROOTS` | cwd + project root | **Filesystem sandbox.** OS-path-separator delimited. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |
| `DEBUG` | `false` | Include tracebacks in tool errors. |
| `KUBECTL_TIMEOUT` | `120` | Seconds before a kubectl call is aborted. |

Never commit `.env`; it is git-ignored.

---

## Running the MCP server

```bash
# stdio — the default. Clients launch this themselves; you rarely run it by hand.
docker2k8s-mcp

# streamable HTTP, for remote or containerised use
docker2k8s-mcp --transport http --host 127.0.0.1 --port 8000   # -> http://127.0.0.1:8000/mcp

# SSE, for older HTTP clients
docker2k8s-mcp --transport sse --port 8000                     # -> http://127.0.0.1:8000/sse
```

Or without installing: `python -m src.main --transport http`.

Logs go to stderr, because stdout carries the MCP protocol on the stdio transport.

### Running the MCP server in Docker

```bash
docker build -t docker2k8s-mcp .

docker run --rm -p 8000:8000 \
  -v "$HOME/.kube:/home/mcp/.kube:ro" \
  -v "$PWD/examples:/workspace:ro" \
  -v d2k-generated:/data \
  docker2k8s-mcp
```

The server needs **kubectl and a kubeconfig**, not a Docker daemon — project
inspection is pure filesystem and YAML work, so no Docker-in-Docker is required.

Two caveats when containerising:

- A kubeconfig pointing at `127.0.0.1` (Docker Desktop, kind, minikube) will not
  resolve from inside a container. Either rewrite the server URL to
  `host.docker.internal`, or run the server on the host.
- Projects must be mounted into the container, and `ALLOWED_ROOTS` must include
  the mount point (the image defaults it to `/workspace`).

**For local development, running the server directly on the host is simpler** and
is the recommended path.

---

## Connecting a client

The server is a standard MCP server. Point any client at it.

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "docker2k8s": {
      "command": "/absolute/path/to/docker2k8s-mcp/.venv/bin/docker2k8s-mcp",
      "env": {
        "ALLOWED_ROOTS": "/absolute/path/to/your/projects",
        "KUBE_NAMESPACE": "default"
      }
    }
  }
}
```

On Windows use `...\\.venv\\Scripts\\docker2k8s-mcp.exe` and `;` between roots.

**Claude Code**:

```bash
claude mcp add docker2k8s -- /absolute/path/to/.venv/bin/docker2k8s-mcp
```

**Cursor / VS Code / Windsurf** — same shape as Claude Desktop (`command`, `args`,
`env`), in that editor's MCP settings file.

**Any client, over HTTP** — start the server with `--transport http` and point the
client at `http://127.0.0.1:8000/mcp`.

**From Python:**

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(command="docker2k8s-mcp")
async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("inspect_project", {"path": "examples/fastapi-mysql"})
        print(result.structured_content)
```

The server sends its workflow as MCP `instructions` on initialize, and exposes
`migrate` and `diagnose` prompts, so a client needs no docker2k8s-specific code.

---

## Running the reference client

A minimal OpenAI-driven agent, included to demonstrate the full loop:

```bash
export OPENAI_API_KEY=sk-...

python -m client.agent "Migrate examples/fastapi-mysql to Kubernetes."
python -m client.agent                                    # interactive
python -m client.agent --http http://127.0.0.1:8000/mcp "..."
```

The model picks every tool. The client adds one thing: it prompts you at the
terminal before any deployment, on top of the server's own approval gate.

---

## Requirements

### Docker

Only needed to *build and run* the example app, and to build images the cluster
will pull. The MCP server itself does not talk to the Docker daemon.

### Kubernetes

Target a local cluster first. Docker Desktop is the assumed default.

1. Docker Desktop → **Settings → Kubernetes → Enable Kubernetes** → Apply & restart.
2. Verify:

```bash
kubectl cluster-info
kubectl get nodes
```

You should see a control plane URL and at least one `Ready` node. If
`kubectl cluster-info` fails, every deployment tool will fail too — check this
first. `get_cluster_info` reports the same thing through MCP.

kind and minikube also work; set `KUBE_CONTEXT` accordingly.

---

## Example migration

[`examples/fastapi-mysql/`](examples/fastapi-mysql/) is a FastAPI service with a
MySQL database: a `/health` endpoint, a database connection, environment
variables, Compose networking, a persistent volume, healthchecks, an init-script
bind mount, and two replicas. It is deliberately chosen to show why this
migration is not a mechanical translation.

Ask any connected client:

> Migrate examples/fastapi-mysql to Kubernetes.

The agent inspects, analyses, plans, generates and validates, then shows you
something like:

```text
Resources:  Deployment/api  Service/api  ConfigMap/api-config  Secret/api-secret
            StatefulSet/mysql  Service/mysql  ConfigMap/mysql-config  Secret/mysql-secret
Ports:      api: NodePort 8000 -> container 8000
            mysql: ClusterIP 3306 -> container 3306
Secrets:    DB_PASSWORD, MYSQL_PASSWORD, MYSQL_ROOT_PASSWORD
Volumes:    mysql: mysql-data -> /var/lib/mysql (1Gi)
Health:     api: httpGet readiness on /health
            mysql: exec readiness

Warnings:
  SECRETS_DETECTED  Generated Secrets contain placeholders, never real values.
  BIND_MOUNT        './initdb' has no Kubernetes equivalent.
  DEPENDS_ON        Kubernetes does not order pod startup; the app must retry.
  LOCAL_BUILD       The cluster cannot build images.

Manual steps:
  - docker build -t fastapi-mysql-api:local examples/fastapi-mysql
  - kubectl port-forward svc/api 8000:8000
  - Decide how to provide './initdb'
```

…then asks whether to deploy. Before saying yes:

```bash
# 1. Build the image so the cluster can find it (Docker Desktop shares its store)
docker build -t fastapi-mysql-api:local examples/fastapi-mysql

# 2. Fill in the placeholder secrets
kubectl create secret generic mysql-secret \
  --from-literal=MYSQL_ROOT_PASSWORD='...' \
  --from-literal=MYSQL_PASSWORD='...' \
  --dry-run=client -o yaml | kubectl apply -f -
```

Then approve. The agent applies the manifests, verifies the rollout, and
diagnoses anything that fails.

```bash
kubectl port-forward svc/api 8000:8000
curl http://localhost:8000/health
```

---

## Available MCP tools

### Inspection (read-only)

| Tool | Purpose |
|---|---|
| `inspect_project` | Dockerfiles, Compose file, `.env`, source dirs, service names. Start here. |
| `inspect_dockerfile` | Stages, base images, `EXPOSE`, `WORKDIR`, `USER`, `ENTRYPOINT`/`CMD`, `HEALTHCHECK`. |
| `inspect_compose` | Normalised services: ports, environment, volumes, `depends_on`, healthchecks, resources. |
| `inspect_environment` | Environment variables classified as config or secret. Values of secrets are never returned. |

### Analysis and planning (read-only)

| Tool | Purpose |
|---|---|
| `analyze_project` | How each Docker concept maps to Kubernetes; warnings and blockers. |
| `create_migration_plan` | The reviewable plan: workloads, ports, config/secrets, volumes, probes, manual steps. |

### Generation and validation

| Tool | Purpose |
|---|---|
| `generate_manifests` | Writes YAML to `generated/<project>/k8s`. Writes files only. |
| `validate_manifests` | Selector, port, probe, reference, duplicate and naming checks. |

### Cluster (read-only, except where noted)

| Tool | Purpose |
|---|---|
| `get_cluster_info` | Is a cluster reachable? Nodes and namespaces. |
| `apply_manifests` | **Destructive.** Requires `approved=true`; refuses invalid manifests. `dry_run=true` is safe. |
| `get_deployment_status` | Desired vs ready replicas and rollout conditions. |
| `get_pods` | Phase, readiness, restarts, detected problems. |
| `get_pod_logs` | Container logs; `previous=true` for a crash loop. |
| `get_services` | Type, ports, selector, endpoint count. |
| `get_events` | Recent events, including warnings. |
| `verify_deployment` | All post-deployment checks in one call. |
| `diagnose_deployment` | Problem, evidence, likely cause, suggested fix. |

---

## Security considerations

**No arbitrary execution.** There is no `execute_shell` or `execute_kubectl`
tool. Every cluster capability is a separate named tool with a typed schema.
kubectl is invoked with an explicit argv list, never through a shell, and only
from `k8s_service.py`. Names are validated against DNS-1123 before they reach a
command line, so a crafted argument cannot become a flag.

**Filesystem sandbox.** Paths are resolved (collapsing `..`) and then checked for
containment inside `ALLOWED_ROOTS`, which defaults to the working directory and
the project root. Anything outside is refused. Set `ALLOWED_ROOTS` explicitly when
running the server for someone else.

**Secrets never leave the boundary.** Values matching credential patterns — and
connection strings with embedded passwords — are replaced with `<REDACTED>` when
parsed, so they never reach the model, the logs or a manifest. Generated Secrets
contain `<REQUIRED_SECRET>` placeholders, and validation warns while they remain.
Fill them in with `kubectl create secret` or a secrets manager.

Kubernetes Secrets are base64-encoded, not encrypted. For production, enable
encryption at rest or use an external secrets operator.

**Deployment requires approval.** `apply_manifests` refuses without
`approved=true`, and refuses manifests that fail validation. The tool is annotated
`destructive_hint`, so clients that surface annotations will prompt as well.

**Errors do not leak internals.** Domain errors return a message and a hint;
anything unexpected returns a generic failure and logs the traceback server-side.
Set `DEBUG=true` to include tracebacks in tool output while developing.

---

## Development

```text
src/
  main.py          entry point, transports
  server.py        MCP server, tool registration, instructions
  config.py        settings
  schemas.py       Pydantic models
  security.py      path sandbox, secret detection
  errors.py        domain errors
  tools/           thin MCP wrappers
  services/        business logic
  generators/      plan -> YAML
  validators/      YAML -> issues
client/agent.py    reference LLM agent
examples/          example application
tests/             unit + integration tests
```

Rules of the codebase:

- Tools stay thin; logic lives in services.
- All Kubernetes access goes through `k8s_service.py`.
- All path access goes through `resolve_project_path`.
- Never log or return a secret value.
- Use the standard `logging` module, not `print`.

---

## Testing

```bash
pip install -e ".[dev]"

pytest                    # unit tests; no Docker or Kubernetes needed
pytest -m integration     # requires a running cluster
pytest --cov=src          # with coverage
```

Unit tests build small Docker projects in a `tmp_path` fixture, so they are fast
and hermetic. Integration tests are marked and excluded by default.

---

## Roadmap

Not implemented; documented as future work.

| Version | Scope |
|---|---|
| **V2** | Nginx configuration → Kubernetes Ingress migration |
| **V3** | AWS EKS support |
| **V4** | AWS infrastructure inspection |
| **V5** | Production readiness checker (probes, limits, PDBs, security contexts) |
| **V6** | Automatic remediation, with explicit approval |

The local Docker → Kubernetes pipeline is the priority; AWS work starts only once
it is solid end to end.

---

## License

MIT
# docker2k8s-mcp-server

[![M8ven Score](https://m8ven.ai/badge/mcp/vishalnotfound/docker2k8s-mcp-server)](https://m8ven.ai/mcp/vishalnotfound/docker2k8s-mcp-server)

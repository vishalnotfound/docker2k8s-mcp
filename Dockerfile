# Container image for the docker2k8s MCP server.
#
# The server needs kubectl and a kubeconfig to reach a cluster; it does NOT need
# a Docker daemon, because project inspection is pure filesystem + YAML work.
# See "Running the MCP server in Docker" in the README for how to mount both.

FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
COPY client ./client
RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.12-slim

ARG KUBECTL_VERSION=v1.31.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GENERATED_DIR=/data/generated \
    ALLOWED_ROOTS=/workspace \
    KUBECTL_PATH=/usr/local/bin/kubectl

# kubectl is the only external binary the server shells out to.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) karch=amd64 ;; arm64) karch=arm64 ;; *) echo "unsupported arch $arch" >&2; exit 1 ;; esac; \
    curl -fsSLo /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${karch}/kubectl"; \
    chmod +x /usr/local/bin/kubectl; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --create-home --uid 1000 mcp; \
    mkdir -p /data/generated /workspace; \
    chown -R mcp:mcp /data

COPY --from=builder /install /usr/local

WORKDIR /app
USER mcp

# Projects to migrate are mounted read-only at /workspace; generated manifests
# land in /data so they survive as a named volume.
VOLUME ["/data"]

# Default to HTTP so the container is reachable from a client outside it.
# For stdio, override with: docker run -i ... --transport stdio
EXPOSE 8000
ENTRYPOINT ["docker2k8s-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8000"]

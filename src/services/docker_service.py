"""Read and understand a Docker project.

Everything here is pure filesystem + YAML work: no Docker daemon is required to
inspect a project, which keeps the tools usable on any machine.  Compose's
short and long syntaxes are normalised into the models in ``schemas`` so the
rest of the pipeline never has to care which form the author used.
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import Any

import yaml

from ..errors import ComposeFileNotFound, DockerfileNotFound, InvalidComposeFile, ProjectNotFound
from ..schemas import (
    ComposeInspection,
    ComposeService,
    DockerfileInspection,
    DockerfileStage,
    EnvironmentInspection,
    EnvVarInfo,
    HealthCheck,
    PortMapping,
    ProjectInspection,
    ResourceLimits,
    VolumeMount,
)
from ..security import classify_env, is_secret_key, redact, resolve_project_path

logger = logging.getLogger(__name__)

COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

DEPENDENCY_FILENAMES = (
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "poetry.lock",
    "package.json",
    "yarn.lock",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
)

CONFIG_EXTENSIONS = (".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".json")

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    "generated",
}

#: Images that carry state and therefore want a StatefulSet + PVC, not a Deployment.
STATEFUL_IMAGE_HINTS = (
    "mysql",
    "mariadb",
    "postgres",
    "postgis",
    "mongo",
    "redis",
    "elasticsearch",
    "opensearch",
    "cassandra",
    "rabbitmq",
    "kafka",
    "zookeeper",
    "couchdb",
    "influxdb",
    "clickhouse",
    "minio",
    "etcd",
)


# --------------------------------------------------------------------------
# Project layout
# --------------------------------------------------------------------------
def inspect_project(path: str) -> ProjectInspection:
    """Identify the Docker-relevant files in a project directory."""
    root = resolve_project_path(path)
    if not root.is_dir():
        raise ProjectNotFound(
            f"'{root}' is not a directory.",
            hint="Pass the directory that holds the Dockerfile / docker-compose.yml.",
        )

    logger.info("Inspecting project: %s", root)

    dockerfiles: list[str] = []
    dependency_files: list[str] = []
    config_files: list[str] = []
    env_files: list[str] = []
    source_dirs: list[str] = []
    compose_path: Path | None = None

    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            if _looks_like_source_dir(entry):
                source_dirs.append(entry.name)
            continue
        name = entry.name
        if name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile"):
            dockerfiles.append(_rel(entry, root))
        elif compose_path is None and name in COMPOSE_FILENAMES:
            compose_path = entry
        elif name == ".env" or name.startswith(".env"):
            env_files.append(name)
        elif name in DEPENDENCY_FILENAMES:
            dependency_files.append(name)
        elif entry.suffix in CONFIG_EXTENSIONS and name not in COMPOSE_FILENAMES:
            config_files.append(name)

    # Dockerfiles are often one level down (e.g. api/Dockerfile).
    for sub in sorted(root.iterdir()):
        if sub.is_dir() and sub.name not in SKIP_DIRS and not sub.name.startswith("."):
            for candidate in sorted(sub.glob("Dockerfile*")):
                dockerfiles.append(_rel(candidate, root))

    services: list[str] = []
    notes: list[str] = []
    if compose_path is not None:
        try:
            services = [svc.name for svc in inspect_compose(str(compose_path)).services]
        except InvalidComposeFile as exc:
            notes.append(f"Compose file present but could not be parsed: {exc.message}")
    else:
        notes.append(
            "No Compose file found. A single Dockerfile can still be migrated, but "
            "ports and environment must be supplied manually."
        )

    if env_files:
        notes.append("Environment files were found; their values are never returned verbatim.")

    inspection = ProjectInspection(
        project_path=str(root),
        project_name=_sanitize_name(root.name),
        dockerfile=bool(dockerfiles),
        dockerfile_paths=dockerfiles,
        compose_file=compose_path is not None,
        compose_path=str(compose_path) if compose_path else None,
        env_file=bool(env_files),
        env_files=env_files,
        services=services,
        source_dirs=source_dirs,
        dependency_files=dependency_files,
        config_files=config_files,
        notes=notes,
    )
    logger.info(
        "Found %d Docker service(s), %d Dockerfile(s)", len(services), len(dockerfiles)
    )
    return inspection


def _looks_like_source_dir(path: Path) -> bool:
    """A directory counts as source if it holds code or a package marker."""
    patterns = ("*.py", "*.js", "*.ts", "*.go", "*.java", "*.rb", "*.rs", "*.php")
    for pattern in patterns:
        if next(path.glob(pattern), None) is not None:
            return True
    return (path / "__init__.py").exists() or (path / "package.json").exists()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# Dockerfile
# --------------------------------------------------------------------------
def inspect_dockerfile(path: str) -> DockerfileInspection:
    """Parse a Dockerfile into the facts that matter for a migration.

    ``path`` may point at the Dockerfile itself or at a directory containing one.
    """
    target = resolve_project_path(path)
    if target.is_dir():
        candidate = target / "Dockerfile"
        if not candidate.exists():
            found = next(iter(sorted(target.glob("Dockerfile*"))), None)
            if found is None:
                raise DockerfileNotFound(
                    f"No Dockerfile in '{target}'.",
                    hint="Call inspect_project first to list dockerfile_paths.",
                )
            candidate = found
        target = candidate

    logger.info("Inspecting Dockerfile: %s", target)
    lines = _join_continuations(target.read_text(encoding="utf-8", errors="replace"))

    stages: list[DockerfileStage] = []
    ports: list[int] = []
    env_keys: list[str] = []
    build_args: list[str] = []
    volumes: list[str] = []
    notes: list[str] = []
    workdir: str | None = None
    user: str | None = None
    entrypoint: list[str] | None = None
    cmd: list[str] | None = None
    healthcheck: str | None = None

    for line in lines:
        instruction, _, remainder = line.partition(" ")
        instruction = instruction.upper()
        remainder = remainder.strip()
        if not remainder and instruction not in {"USER"}:
            continue

        if instruction == "FROM":
            parts = remainder.split()
            base = parts[0]
            alias = parts[2] if len(parts) >= 3 and parts[1].upper() == "AS" else None
            stages.append(DockerfileStage(index=len(stages), base_image=base, name=alias))
        elif instruction == "EXPOSE":
            for token in remainder.split():
                port = token.split("/")[0]
                if port.isdigit():
                    ports.append(int(port))
        elif instruction == "ENV":
            env_keys.extend(_dockerfile_env_keys(remainder))
        elif instruction == "ARG":
            build_args.append(remainder.split("=")[0].strip())
        elif instruction == "WORKDIR":
            workdir = remainder
        elif instruction == "USER":
            user = remainder or None
        elif instruction == "ENTRYPOINT":
            entrypoint = _parse_exec_form(remainder)
        elif instruction == "CMD":
            cmd = _parse_exec_form(remainder)
        elif instruction == "VOLUME":
            volumes.extend(_parse_exec_form(remainder) or [remainder])
        elif instruction == "HEALTHCHECK":
            healthcheck = remainder

    if user is None:
        notes.append(
            "No USER instruction: the container runs as root. Consider a "
            "securityContext with runAsNonRoot in Kubernetes."
        )
    if not ports:
        notes.append(
            "No EXPOSE instruction: the listening port must come from Compose or be "
            "supplied manually."
        )
    if any(stage.base_image.endswith(":latest") for stage in stages):
        notes.append("A base image uses the ':latest' tag, which is not reproducible.")

    return DockerfileInspection(
        path=str(target),
        stages=stages,
        final_base_image=stages[-1].base_image if stages else None,
        multi_stage=len(stages) > 1,
        exposed_ports=sorted(set(ports)),
        workdir=workdir,
        user=user,
        entrypoint=entrypoint,
        cmd=cmd,
        env_keys=sorted(set(env_keys)),
        build_args=sorted(set(filter(None, build_args))),
        healthcheck=healthcheck,
        volumes=volumes,
        notes=notes,
    )


def _join_continuations(text: str) -> list[str]:
    """Collapse backslash line-continuations and drop comments/blank lines."""
    joined: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if stripped.endswith("\\"):
            buffer += stripped[:-1].strip() + " "
            continue
        buffer += stripped
        joined.append(buffer.strip())
        buffer = ""
    if buffer.strip():
        joined.append(buffer.strip())
    return joined


def _dockerfile_env_keys(remainder: str) -> list[str]:
    """Handle both `ENV KEY=value KEY2=value2` and legacy `ENV KEY value`."""
    if "=" not in remainder:
        return [remainder.split()[0]] if remainder.split() else []
    keys = []
    try:
        tokens = shlex.split(remainder)
    except ValueError:
        tokens = remainder.split()
    for token in tokens:
        if "=" in token:
            keys.append(token.split("=", 1)[0])
    return keys


def _parse_exec_form(remainder: str) -> list[str] | None:
    """Parse a Dockerfile exec form (JSON array) or shell form into a list."""
    remainder = remainder.strip()
    if not remainder:
        return None
    if remainder.startswith("["):
        try:
            parsed = yaml.safe_load(remainder)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except yaml.YAMLError:
            pass
    try:
        return shlex.split(remainder)
    except ValueError:
        return remainder.split()


# --------------------------------------------------------------------------
# Compose
# --------------------------------------------------------------------------
def find_compose_file(path: str | Path) -> Path:
    """Locate the Compose file for a directory, or validate a direct path."""
    target = resolve_project_path(path)
    if target.is_file():
        return target
    for name in COMPOSE_FILENAMES:
        candidate = target / name
        if candidate.exists():
            return candidate
    raise ComposeFileNotFound(
        f"No Compose file in '{target}'.",
        hint=f"Looked for: {', '.join(COMPOSE_FILENAMES)}.",
    )


def inspect_compose(path: str) -> ComposeInspection:
    """Parse a Compose file into normalised service descriptions.

    Secret-looking environment values are redacted here, at the boundary, so no
    downstream caller can accidentally leak them.
    """
    compose_path = find_compose_file(path)
    logger.info("Inspecting Compose file: %s", compose_path)

    try:
        raw = yaml.safe_load(compose_path.read_text(encoding="utf-8", errors="replace"))
    except yaml.YAMLError as exc:
        raise InvalidComposeFile(
            f"Could not parse '{compose_path}': {exc}",
            hint="Fix the YAML syntax and retry.",
        ) from exc

    if not isinstance(raw, dict):
        raise InvalidComposeFile(
            f"'{compose_path}' does not contain a YAML mapping.",
            hint="A Compose file must start with a top-level 'services:' key.",
        )

    services_raw = raw.get("services")
    if not isinstance(services_raw, dict) or not services_raw:
        raise InvalidComposeFile(
            f"'{compose_path}' has no 'services' section.",
            hint="Add at least one service definition.",
        )

    warnings: list[str] = []
    services: list[ComposeService] = []
    for name, definition in services_raw.items():
        if not isinstance(definition, dict):
            warnings.append(f"Service '{name}' is not a mapping and was skipped.")
            continue
        services.append(_parse_service(str(name), definition, warnings))

    named_volumes = sorted((raw.get("volumes") or {}).keys()) if isinstance(raw.get("volumes"), dict) else []
    networks = sorted((raw.get("networks") or {}).keys()) if isinstance(raw.get("networks"), dict) else []

    if raw.get("version"):
        warnings.append(
            "The 'version' key is obsolete in the Compose specification and is ignored."
        )

    logger.info("Parsed %d Compose service(s)", len(services))
    return ComposeInspection(
        compose_path=str(compose_path),
        project_name=_sanitize_name(compose_path.parent.name),
        services=services,
        named_volumes=named_volumes,
        networks=networks,
        warnings=warnings,
    )


def _parse_service(name: str, definition: dict[str, Any], warnings: list[str]) -> ComposeService:
    build = definition.get("build")
    build_context: str | None = None
    dockerfile: str | None = None
    build_args: list[str] = []
    if isinstance(build, str):
        build_context = build
    elif isinstance(build, dict):
        build_context = build.get("context")
        dockerfile = build.get("dockerfile")
        build_args = list(_as_env_dict(build.get("args")).keys())

    environment = _as_env_dict(definition.get("environment"))
    secret_keys = sorted(k for k, v in environment.items() if classify_env(k, v))
    safe_environment = {k: redact(k, v) for k, v in environment.items()}

    volumes = [_parse_volume(item, warnings, name) for item in definition.get("volumes") or []]
    volumes = [v for v in volumes if v is not None]

    image = definition.get("image")
    stateful = _is_stateful(image, volumes, built_locally=build_context is not None)

    if definition.get("privileged"):
        warnings.append(
            f"Service '{name}' runs privileged; Kubernetes requires an explicit "
            "securityContext and a permissive PodSecurity policy."
        )

    return ComposeService(
        name=name,
        image=image,
        build_context=build_context,
        dockerfile=dockerfile,
        build_args=build_args,
        command=_as_command(definition.get("command")),
        entrypoint=_as_command(definition.get("entrypoint")),
        ports=_parse_ports(definition.get("ports"), warnings, name),
        expose=[int(str(p).split("/")[0]) for p in definition.get("expose") or [] if str(p).split("/")[0].isdigit()],
        environment=safe_environment,
        secret_keys=secret_keys,
        env_files=_as_list(definition.get("env_file")),
        volumes=volumes,
        depends_on=_parse_depends_on(definition.get("depends_on")),
        healthcheck=_parse_healthcheck(definition.get("healthcheck")),
        restart=definition.get("restart"),
        networks=_parse_networks(definition.get("networks")),
        replicas=_parse_replicas(definition.get("deploy")),
        resources=_parse_resources(definition.get("deploy")),
        user=str(definition["user"]) if definition.get("user") is not None else None,
        working_dir=definition.get("working_dir"),
        privileged=bool(definition.get("privileged")),
        stateful=stateful,
    )


def _is_stateful(image: str | None, volumes: list[VolumeMount], built_locally: bool) -> bool:
    """A service is stateful if it is a known datastore or keeps a named volume.

    The datastore check only applies to pulled upstream images: a locally built
    image called ``fastapi-mysql-api`` is our application, not a database.
    """
    if any(v.kind == "named" for v in volumes):
        return True
    if image and not built_locally:
        base = image.split("/")[-1].split(":")[0].lower()
        if any(base == hint or base.startswith(hint) for hint in STATEFUL_IMAGE_HINTS):
            return True
    return False


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _as_command(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        return shlex.split(str(value))
    except ValueError:
        return str(value).split()


def _as_env_dict(value: Any) -> dict[str, str | None]:
    """Compose accepts both `KEY: value` mappings and `- KEY=value` lists."""
    result: dict[str, str | None] = {}
    if isinstance(value, dict):
        for key, val in value.items():
            result[str(key)] = None if val is None else str(val)
    elif isinstance(value, list):
        for item in value:
            text = str(item)
            if "=" in text:
                key, _, val = text.partition("=")
                result[key.strip()] = val
            else:
                # `- KEY` means "inherit from the host environment".
                result[text.strip()] = None
    return result


_PORT_RE = re.compile(
    # The host-IP group must contain dots or brackets, otherwise a plain
    # "8000:8000" would have its published port eaten as an address.
    r"^(?:(?P<host_ip>\[[^\]]+\]|\d{1,3}(?:\.\d{1,3}){3}):)?"
    r"(?:(?P<host>\d+(?:-\d+)?):)?"
    r"(?P<container>\d+(?:-\d+)?)"
    r"(?:/(?P<proto>\w+))?$"
)


def _parse_ports(value: Any, warnings: list[str], service: str) -> list[PortMapping]:
    ports: list[PortMapping] = []
    for item in value or []:
        if isinstance(item, dict):  # long syntax
            target = item.get("target")
            if target is None:
                continue
            ports.append(
                PortMapping(
                    container_port=int(target),
                    host_port=int(item["published"]) if item.get("published") else None,
                    protocol=str(item.get("protocol", "tcp")),
                )
            )
            continue
        match = _PORT_RE.match(str(item).strip())
        if not match:
            warnings.append(f"Service '{service}': could not parse port mapping '{item}'.")
            continue
        container = match.group("container")
        host = match.group("host")
        if "-" in container:  # a port range
            warnings.append(
                f"Service '{service}': port range '{item}' was expanded to its first port only."
            )
            container = container.split("-")[0]
            host = host.split("-")[0] if host else None
        ports.append(
            PortMapping(
                container_port=int(container),
                host_port=int(host) if host else None,
                protocol=match.group("proto") or "tcp",
            )
        )
    return ports


def _parse_volume(item: Any, warnings: list[str], service: str) -> VolumeMount | None:
    if isinstance(item, dict):  # long syntax
        target = item.get("target")
        if not target:
            return None
        vol_type = item.get("type", "volume")
        kind = {"volume": "named", "bind": "bind", "tmpfs": "tmpfs"}.get(vol_type, "named")
        return VolumeMount(
            target=str(target),
            source=item.get("source"),
            kind=kind,  # type: ignore[arg-type]
            read_only=bool(item.get("read_only")),
        )

    text = str(item)
    parts = text.split(":")
    if len(parts) == 1:
        return VolumeMount(target=parts[0], source=None, kind="anonymous")

    # Windows paths look like C:\data:/var/lib -> re-join the drive letter.
    if len(parts[0]) == 1 and parts[0].isalpha():
        parts = [f"{parts[0]}:{parts[1]}"] + parts[2:]

    source, target = parts[0], parts[1]
    read_only = len(parts) > 2 and "ro" in parts[2].split(",")
    is_bind = source.startswith((".", "/", "~")) or (len(source) > 1 and source[1] == ":")
    if is_bind:
        warnings.append(
            f"Service '{service}': bind mount '{source}' has no direct Kubernetes "
            "equivalent and needs a storage strategy."
        )
    return VolumeMount(
        target=target,
        source=source,
        kind="bind" if is_bind else "named",
        read_only=read_only,
    )


def _parse_depends_on(value: Any) -> list[str]:
    if isinstance(value, dict):  # long syntax with conditions
        return [str(k) for k in value]
    return _as_list(value)


def _parse_networks(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(k) for k in value]
    return _as_list(value)


def _parse_healthcheck(value: Any) -> HealthCheck | None:
    if not isinstance(value, dict):
        return None
    if value.get("disable"):
        return HealthCheck(disabled=True)
    test = value.get("test")
    test_list = [str(t) for t in test] if isinstance(test, list) else (["CMD-SHELL", str(test)] if test else [])
    return HealthCheck(
        test=test_list,
        interval_seconds=_duration_seconds(value.get("interval")),
        timeout_seconds=_duration_seconds(value.get("timeout")),
        retries=int(value["retries"]) if value.get("retries") else None,
        start_period_seconds=_duration_seconds(value.get("start_period")),
    )


_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|us|ns|s|m|h)?")
_DURATION_FACTORS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def _duration_seconds(value: Any) -> int | None:
    """Convert a Compose duration ('30s', '1m30s') to whole seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    total = 0.0
    matched = False
    for amount, unit in _DURATION_RE.findall(str(value)):
        matched = True
        total += float(amount) * _DURATION_FACTORS.get(unit or None, 1.0)
    return max(1, int(total)) if matched else None


def _parse_replicas(deploy: Any) -> int | None:
    if isinstance(deploy, dict) and deploy.get("replicas") is not None:
        return int(deploy["replicas"])
    return None


def _parse_resources(deploy: Any) -> ResourceLimits | None:
    if not isinstance(deploy, dict):
        return None
    resources = deploy.get("resources")
    if not isinstance(resources, dict):
        return None
    limits = resources.get("limits") or {}
    reservations = resources.get("reservations") or {}
    result = ResourceLimits(
        cpu_limit=str(limits["cpus"]) if limits.get("cpus") else None,
        memory_limit=_normalise_memory(limits.get("memory")),
        cpu_request=str(reservations["cpus"]) if reservations.get("cpus") else None,
        memory_request=_normalise_memory(reservations.get("memory")),
    )
    return result if any(result.model_dump().values()) else None


def _normalise_memory(value: Any) -> str | None:
    """Compose uses '512M'; Kubernetes wants '512Mi'."""
    if value is None:
        return None
    text = str(value).strip()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([kmgKMG])?[bB]?$", text)
    if not match:
        return text
    amount, unit = match.groups()
    amount = amount.rstrip("0").rstrip(".") if "." in amount else amount
    if not unit:
        return amount
    return f"{amount}{unit.upper()}i"


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------
def inspect_environment(path: str) -> EnvironmentInspection:
    """List environment variables across .env files and Compose, without values.

    Only non-secret values are returned; secret values are never read out.
    """
    root = resolve_project_path(path)
    base_dir = root if root.is_dir() else root.parent
    logger.info("Inspecting environment: %s", base_dir)

    variables: dict[str, EnvVarInfo] = {}
    files: list[str] = []
    warnings: list[str] = []

    for env_path in sorted(base_dir.glob(".env*")):
        if not env_path.is_file():
            continue
        files.append(env_path.name)
        if env_path.name == ".env":
            warnings.append(
                "A committed '.env' file was found. Ensure it is git-ignored; its values "
                "are not returned by this tool."
            )
        for key, value in _parse_env_file(env_path).items():
            secret = classify_env(key, value)
            variables[key] = EnvVarInfo(
                key=key,
                is_secret=secret,
                source=env_path.name,
                has_value=value is not None and value != "",
                value=None if secret else value,
            )

    try:
        compose = inspect_compose(str(base_dir))
    except (ComposeFileNotFound, InvalidComposeFile):
        compose = None
    if compose is not None:
        for service in compose.services:
            for key, value in service.environment.items():
                secret = key in service.secret_keys
                variables.setdefault(
                    key,
                    EnvVarInfo(
                        key=key,
                        is_secret=secret,
                        source=f"compose:{service.name}",
                        has_value=value is not None,
                        value=None if secret else value,
                    ),
                )

    ordered = sorted(variables.values(), key=lambda v: v.key)
    secret_count = sum(1 for v in ordered if v.is_secret)
    if secret_count:
        warnings.append(
            f"{secret_count} variable(s) look like credentials and will become a "
            "Kubernetes Secret with placeholder values."
        )

    return EnvironmentInspection(
        project_path=str(base_dir),
        files=files,
        variables=ordered,
        secret_count=secret_count,
        config_count=len(ordered) - secret_count,
        warnings=warnings,
    )


def _parse_env_file(path: Path) -> dict[str, str | None]:
    """Minimal dotenv parser: KEY=value, with optional 'export' and quotes."""
    result: dict[str, str | None] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _sanitize_name(name: str) -> str:
    """Turn a directory name into a DNS-1123 compatible Kubernetes name."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "app"


__all__ = [
    "inspect_project",
    "inspect_dockerfile",
    "inspect_compose",
    "inspect_environment",
    "find_compose_file",
    "is_secret_key",
]

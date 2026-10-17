"""Read and understand a Docker project.

Everything here is pure filesystem + YAML work: no Docker daemon is required to
inspect a project, which keeps the tools usable on any machine.  Compose's
short and long syntaxes are normalised into the models in ``schemas`` so the
rest of the pipeline never has to care which form the author used.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any, Iterator

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
from ..security import REDACTED, classify_env, is_secret_key, resolve_project_path

logger = logging.getLogger(__name__)

COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

OVERRIDE_FILENAMES = (
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "compose.override.yml",
    "compose.override.yaml",
)

#: Files read for Compose ${VAR} interpolation, in order of preference.
INTERPOLATION_ENV_FILES = (".env", ".env.example", ".env.sample")

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

#: Image names (last path segment, tag stripped) of datastores that carry state
#: and therefore want a StatefulSet. Exact matches only: mongo-express,
#: redis-commander and mysqld-exporter are stateless companions.
STATEFUL_IMAGES = frozenset(
    {
        "mysql",
        "mariadb",
        "percona",
        "percona-server",
        "postgres",
        "postgresql",
        "postgis",
        "timescaledb",
        "mongo",
        "mongodb",
        "redis",
        "redis-stack",
        "redis-stack-server",
        "valkey",
        "keydb",
        "elasticsearch",
        "opensearch",
        "cassandra",
        "scylla",
        "rabbitmq",
        "kafka",
        "cp-kafka",
        "zookeeper",
        "cp-zookeeper",
        "couchdb",
        "couchbase",
        "influxdb",
        "clickhouse",
        "clickhouse-server",
        "minio",
        "etcd",
        "neo4j",
    }
)

#: Service keys this tool translates into Kubernetes.
_TRANSLATED_KEYS = frozenset(
    {
        "image",
        "build",
        "command",
        "entrypoint",
        "ports",
        "expose",
        "environment",
        "env_file",
        "volumes",
        "tmpfs",
        "depends_on",
        "healthcheck",
        "restart",
        "networks",
        "deploy",
        "user",
        "working_dir",
        "privileged",
        "cap_add",
        "cap_drop",
    }
)

#: Service keys that have no Kubernetes meaning worth warning about.
_HARMLESS_KEYS = frozenset(
    {"container_name", "labels", "logging", "platform", "pull_policy", "stdin_open", "tty"}
)

#: Top-level keys that change the application but are not translated.
_UNTRANSLATED_TOP_LEVEL = {
    "include": "other Compose files are included; migrate them separately",
    "configs": "Compose configs are not converted; mount them from a ConfigMap",
    "secrets": "Compose secrets are not converted; create Kubernetes Secrets for them",
}


# --------------------------------------------------------------------------
# YAML
# --------------------------------------------------------------------------
class _ComposeLoader(yaml.SafeLoader):
    """A SafeLoader with YAML 1.2 core-schema scalars, as Docker Compose uses.

    PyYAML implements YAML 1.1, where ``restart: no`` is the boolean False,
    ``FLAG: yes`` is True and an unquoted port mapping ``22:22`` is the base-60
    integer 1342. Compose reads all three as strings.
    """


_BOOL, _INT, _FLOAT, _TIMESTAMP = (
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:timestamp",
)
_ComposeLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in {_BOOL, _INT, _FLOAT, _TIMESTAMP}]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_ComposeLoader.add_implicit_resolver(
    _BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)
_ComposeLoader.add_implicit_resolver(
    _INT, re.compile(r"^(?:[-+]?(?:0|[1-9][0-9]*)|0o[0-7]+|0x[0-9a-fA-F]+)$"), list("-+0123456789")
)
_ComposeLoader.add_implicit_resolver(
    _FLOAT,
    re.compile(
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
    ),
    list("-+0123456789."),
)


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8", errors="replace") as stream:
        return yaml.load(stream, Loader=_ComposeLoader)  # noqa: S506 - SafeLoader subclass


# --------------------------------------------------------------------------
# Interpolation
# --------------------------------------------------------------------------
#: $$, ${VAR}, ${VAR:-default}, ${VAR-default}, ${VAR:?err}, ${VAR:+alt} and $VAR.
#: The argument may hold one nested ${...}, as in ${A:-${B}}.
_INTERPOLATION_RE = re.compile(
    r"\$(?:(?P<escaped>\$)"
    r"|\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?[-?+])(?P<arg>(?:\$\{[^{}]*\}|[^}])*))?\}"
    r"|(?P<named>[A-Za-z_][A-Za-z0-9_]*))"
)


class Interpolator:
    """Resolve Compose ``${VAR}`` references from the project's .env file.

    Secret values are never loaded. A reference to a secret variable is left
    as written and recorded in ``secret_refs``, so the caller can route the
    value to a Kubernetes Secret instead of a ConfigMap, image name or command.
    """

    def __init__(self, values: dict[str, str], secret_names: set[str], source: str | None) -> None:
        self.values = values
        self.secret_names = secret_names
        self.source = source
        self.missing: set[str] = set()
        self.secret_refs: set[str] = set()

    @classmethod
    def for_project(cls, root: Path) -> Interpolator:
        for candidate in INTERPOLATION_ENV_FILES:
            path = root / candidate
            if not path.is_file():
                continue
            values: dict[str, str] = {}
            secret_names: set[str] = set()
            for key, value in iter_env_file(path):
                if classify_env(key, value):
                    secret_names.add(key)  # the value is dropped right here
                else:
                    values[key] = value
            return cls(values, secret_names, candidate)
        return cls({}, set(), None)

    def reset(self) -> None:
        self.missing = set()
        self.secret_refs = set()

    def text(self, text: str) -> str:
        return _INTERPOLATION_RE.sub(self._replace, text) if "$" in text else text

    def tree(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        if isinstance(value, dict):
            return {key: self.tree(item) for key, item in value.items()}
        return value

    def _replace(self, match: re.Match[str]) -> str:
        if match.group("escaped"):
            return "$"
        name = match.group("braced") or match.group("named")
        if name in self.secret_names:
            self.secret_refs.add(name)
            return match.group(0)
        value = self.values.get(name)
        op, arg = match.group("op"), match.group("arg") or ""
        if op in {":-", "-"}:
            unset = value is None or (op == ":-" and value == "")
            return self.text(arg) if unset else value  # type: ignore[return-value]
        if op in {":+", "+"}:
            is_set = value is not None and (op == "+" or value != "")
            return self.text(arg) if is_set else ""
        if value is None or (op == ":?" and value == ""):
            self.missing.add(name)
            return match.group(0)
        return value


# --------------------------------------------------------------------------
# Project layout
# --------------------------------------------------------------------------
def _is_dockerfile_name(name: str) -> bool:
    lowered = name.lower()
    return lowered == "dockerfile" or lowered.startswith("dockerfile.") or lowered.endswith(".dockerfile")


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
    subdirs: list[Path] = []
    compose_path: Path | None = None

    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            subdirs.append(entry)
            if _looks_like_source_dir(entry):
                source_dirs.append(entry.name)
            continue
        name = entry.name
        if _is_dockerfile_name(name):
            dockerfiles.append(_rel(entry, root))
        elif compose_path is None and name in COMPOSE_FILENAMES:
            compose_path = entry
        elif name.startswith(".env"):
            env_files.append(name)
        elif name in DEPENDENCY_FILENAMES:
            dependency_files.append(name)
        elif entry.suffix in CONFIG_EXTENSIONS and name not in COMPOSE_FILENAMES:
            config_files.append(name)

    # Dockerfiles are often one level down (e.g. api/Dockerfile).
    for sub in subdirs:
        dockerfiles.extend(
            _rel(candidate, root)
            for candidate in sorted(sub.iterdir())
            if candidate.is_file() and _is_dockerfile_name(candidate.name)
        )

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
def find_dockerfile(directory: Path) -> Path | None:
    """The directory's Dockerfile, matched case-insensitively like Docker on Windows."""
    exact = directory / "Dockerfile"
    if exact.is_file():
        return exact
    candidates = sorted(p for p in directory.iterdir() if p.is_file() and _is_dockerfile_name(p.name))
    plain = [p for p in candidates if p.name.lower() == "dockerfile"]
    return (plain or candidates or [None])[0]


def inspect_dockerfile(path: str) -> DockerfileInspection:
    """Parse a Dockerfile into the facts that matter for a migration.

    ``path`` may point at the Dockerfile itself or at a directory containing one.
    """
    target = resolve_project_path(path)
    if target.is_dir():
        found = find_dockerfile(target)
        if found is None:
            raise DockerfileNotFound(
                f"No Dockerfile in '{target}'.",
                hint="Call inspect_project first to list dockerfile_paths.",
            )
        target = found

    logger.info("Inspecting Dockerfile: %s", target)
    with target.open(encoding="utf-8", errors="replace") as stream:
        lines = _join_continuations(stream)

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
        parts = line.split(None, 1)
        instruction = parts[0].upper()
        remainder = parts[1].strip() if len(parts) > 1 else ""
        if not remainder and instruction != "USER":
            continue

        if instruction == "FROM":
            # Flags such as --platform=$BUILDPLATFORM precede the image.
            tokens = [t for t in remainder.split() if not t.startswith("--")]
            if not tokens:
                continue
            alias = tokens[2] if len(tokens) >= 3 and tokens[1].upper() == "AS" else None
            stages.append(DockerfileStage(index=len(stages), base_image=tokens[0], name=alias))
        elif instruction == "EXPOSE":
            for token in remainder.split():
                port = token.split("/")[0]
                if port.isdigit():
                    ports.append(int(port))
        elif instruction == "ENV":
            env_keys.extend(_dockerfile_env_keys(remainder))
        elif instruction == "ARG":
            build_args.extend(token.split("=", 1)[0] for token in remainder.split())
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
    stage_names = {stage.name for stage in stages if stage.name}
    unpinned = [
        stage.base_image
        for stage in stages
        if stage.base_image not in stage_names and _is_unpinned(stage.base_image)
    ]
    if unpinned:
        notes.append(
            f"Base image(s) {', '.join(sorted(set(unpinned)))} use ':latest' (explicitly or "
            "implicitly), which is not reproducible."
        )

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


def _is_unpinned(image: str) -> bool:
    if image == "scratch" or "@" in image or "$" in image:
        return False
    name = image.rsplit("/", 1)[-1]
    return ":" not in name or name.endswith(":latest")


def _join_continuations(lines: Iterator[str] | Any) -> list[str]:
    """Collapse backslash line-continuations and drop comments/blank lines.

    As in Docker, comment and blank lines inside a continuation are skipped
    rather than ending the instruction.
    """
    joined: list[str] = []
    buffer = ""
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buffer += stripped[:-1].strip() + " "
            continue
        joined.append((buffer + stripped).strip())
        buffer = ""
    if buffer.strip():
        joined.append(buffer.strip())
    return joined


def _dockerfile_env_keys(remainder: str) -> list[str]:
    """Handle both `ENV KEY=value KEY2=value2` and legacy `ENV KEY value`."""
    if "=" not in remainder:
        return [remainder.split()[0]] if remainder.split() else []
    try:
        tokens = shlex.split(remainder)
    except ValueError:
        tokens = remainder.split()
    return [token.split("=", 1)[0] for token in tokens if "=" in token]


def _parse_exec_form(remainder: str) -> list[str] | None:
    """Parse a Dockerfile exec form (JSON array) or shell form into a list."""
    remainder = remainder.strip()
    if not remainder:
        return None
    if remainder.startswith("["):
        try:
            parsed = json.loads(remainder)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except ValueError:
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

    ``${VAR}`` references are resolved from the project's .env the way Compose
    does it, across every field. Secret-looking environment values are redacted
    here, at the boundary, so no downstream caller can accidentally leak them.
    """
    compose_path = find_compose_file(path)
    logger.info("Inspecting Compose file: %s", compose_path)

    try:
        raw = _load_yaml(compose_path)
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

    root = compose_path.parent
    interpolator = Interpolator.for_project(root)
    warnings: list[str] = []

    if raw.get("version"):
        warnings.append(
            "The 'version' key is obsolete in the Compose specification and is ignored."
        )
    for key, why in _UNTRANSLATED_TOP_LEVEL.items():
        if raw.get(key):
            warnings.append(f"Top-level '{key}' is not migrated: {why}.")
    overrides = [name for name in OVERRIDE_FILENAMES if (root / name).is_file()]
    if overrides:
        warnings.append(
            f"{', '.join(overrides)} is not merged. Compose applies it on 'docker compose up'; "
            "fold its changes into the main file or edit the generated manifests."
        )

    services: list[ComposeService] = []
    for name, definition in services_raw.items():
        if not isinstance(definition, dict):
            warnings.append(f"Service '{name}' is not a mapping and was skipped.")
            continue
        try:
            services.append(_parse_service(str(name), definition, warnings, interpolator))
        except (ValueError, TypeError) as exc:  # pydantic's ValidationError is a ValueError
            raise InvalidComposeFile(
                f"Service '{name}' in '{compose_path}' could not be read: {exc}",
                hint="Check the service definition against the Compose specification.",
            ) from exc

    named_volumes = sorted(raw["volumes"]) if isinstance(raw.get("volumes"), dict) else []
    networks = sorted(raw["networks"]) if isinstance(raw.get("networks"), dict) else []
    declared_name = raw.get("name")
    project_name = _sanitize_name(
        interpolator.text(str(declared_name)) if declared_name else root.name
    )

    logger.info("Parsed %d Compose service(s)", len(services))
    return ComposeInspection(
        compose_path=str(compose_path),
        project_name=project_name,
        services=services,
        named_volumes=named_volumes,
        networks=networks,
        env_source=interpolator.source,
        warnings=warnings,
    )


def _parse_service(
    name: str, raw_definition: dict[str, Any], warnings: list[str], interpolator: Interpolator
) -> ComposeService:
    _warn_untranslated(name, raw_definition, warnings)

    environment, secret_keys, env_missing = _parse_environment(
        raw_definition.get("environment"), interpolator
    )

    # Everything else is interpolated as Compose would, except that a secret is
    # never substituted into an image name, command or path.
    interpolator.reset()
    definition = interpolator.tree(
        {key: value for key, value in raw_definition.items() if key != "environment"}
    )
    for secret in sorted(interpolator.secret_refs):
        warnings.append(
            f"Service '{name}' uses secret variable '{secret}' outside 'environment'; it "
            "was left unresolved rather than written into the manifests."
        )
    unresolved = sorted(env_missing | interpolator.missing | interpolator.secret_refs)

    build = definition.get("build")
    build_context: str | None = None
    dockerfile: str | None = None
    build_args: list[str] = []
    if isinstance(build, str):
        build_context = build
    elif isinstance(build, dict):
        # Compose defaults the context to the project directory.
        build_context = str(build.get("context") or ".")
        dockerfile = str(build["dockerfile"]) if build.get("dockerfile") else None
        build_args = list(_as_env_dict(build.get("args")).keys())

    volumes = [_parse_volume(item, warnings, name) for item in definition.get("volumes") or []]
    volumes = [v for v in volumes if v is not None]
    volumes.extend(_parse_tmpfs(definition.get("tmpfs")))

    image = str(definition["image"]) if definition.get("image") else None

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
        expose=[
            port
            for item in definition.get("expose") or []
            if (port := _port_number(item, warnings, name, "expose")) is not None
        ],
        environment=environment,
        secret_keys=secret_keys,
        env_files=_env_file_paths(definition.get("env_file")),
        volumes=volumes,
        depends_on=_parse_depends_on(definition.get("depends_on")),
        healthcheck=_parse_healthcheck(definition.get("healthcheck")),
        restart=_optional_str(definition.get("restart")),
        networks=_parse_networks(definition.get("networks")),
        replicas=_parse_replicas(definition.get("deploy"), warnings, name),
        resources=_parse_resources(definition.get("deploy")),
        user=_optional_str(definition.get("user")),
        working_dir=_optional_str(definition.get("working_dir")),
        privileged=bool(definition.get("privileged")),
        cap_add=_capabilities(definition.get("cap_add")),
        cap_drop=_capabilities(definition.get("cap_drop")),
        stateful=_is_datastore(image, built_locally=build_context is not None),
        unresolved_variables=unresolved,
    )


def _parse_environment(
    value: Any, interpolator: Interpolator
) -> tuple[dict[str, str | None], list[str], set[str]]:
    """Interpolate and classify a service's environment.

    Returns (safe environment, secret keys, unresolved variable names). A value
    is a secret when its key or text looks like a credential, or when it
    references a secret .env variable -- then it is redacted, not resolved.
    """
    environment: dict[str, str | None] = {}
    secret_keys: list[str] = []
    missing: set[str] = set()

    for key, raw in _as_env_dict(value).items():
        if raw is None:
            # `- KEY` inherits the value from the project environment.
            if key in interpolator.secret_names or is_secret_key(key):
                secret_keys.append(key)
                environment[key] = REDACTED
            elif key in interpolator.values:
                environment[key] = interpolator.values[key]
            else:
                missing.add(key)
                environment[key] = None
            continue

        if classify_env(key, raw):
            secret_keys.append(key)
            environment[key] = REDACTED
            continue
        interpolator.reset()
        resolved = interpolator.text(raw)
        if interpolator.secret_refs or classify_env(key, resolved):
            secret_keys.append(key)
            environment[key] = REDACTED
            continue
        missing |= interpolator.missing
        environment[key] = resolved

    return environment, sorted(secret_keys), missing


def _warn_untranslated(name: str, definition: dict[str, Any], warnings: list[str]) -> None:
    if definition.get("profiles"):
        warnings.append(
            f"Service '{name}' belongs to profile(s) {_as_list(definition['profiles'])}; Compose "
            "only starts it when a profile is active, but it is migrated unconditionally."
        )
    if definition.get("extends"):
        warnings.append(
            f"Service '{name}' uses 'extends', which is not resolved; copy the base service's "
            "settings into it before migrating."
        )
    ignored = sorted(
        key
        for key in definition
        if key not in _TRANSLATED_KEYS
        and key not in _HARMLESS_KEYS
        and key not in {"profiles", "extends"}
        and not str(key).startswith("x-")
    )
    if ignored:
        warnings.append(
            f"Service '{name}' sets {', '.join(map(str, ignored))}, which have no automatic "
            "Kubernetes translation. Review them and edit the generated manifests if needed."
        )


def _is_datastore(image: str | None, built_locally: bool) -> bool:
    """True for pulled upstream datastore images.

    A locally built image called ``fastapi-mysql-api`` is our application, not
    a database, so the check only applies to images Compose pulls.
    """
    if not image or built_locally:
        return False
    base = image.rsplit("/", 1)[-1].split("@", 1)[0].split(":", 1)[0].lower()
    return base in STATEFUL_IMAGES


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _env_file_paths(value: Any) -> list[str]:
    """env_file accepts strings and, since Compose 2.24, {path, required} mappings."""
    if isinstance(value, list):
        return [str(item["path"]) if isinstance(item, dict) else str(item) for item in value]
    return _as_list(value)


def _capabilities(value: Any) -> list[str]:
    """Compose accepts NET_ADMIN or CAP_NET_ADMIN; Kubernetes wants NET_ADMIN."""
    return [cap.upper().removeprefix("CAP_") for cap in _as_list(value)]


def _as_command(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        return shlex.split(str(value))
    except ValueError:
        return str(value).split()


def _env_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"  # what Compose passes to the container
    return str(value)


def _as_env_dict(value: Any) -> dict[str, str | None]:
    """Compose accepts both `KEY: value` mappings and `- KEY=value` lists."""
    result: dict[str, str | None] = {}
    if isinstance(value, dict):
        for key, val in value.items():
            result[str(key)] = _env_value(val)
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
    r"(?:(?P<host>\d+(?:-\d+)?)?:)?"
    r"(?P<container>\d+(?:-\d+)?)"
    r"(?:/(?P<proto>\w+))?$"
)


def _port_number(value: Any, warnings: list[str], service: str, what: str) -> int | None:
    """Read a port or a port range (first port only), warning instead of failing."""
    text = str(value).strip().split("/", 1)[0]
    first, _, rest = text.partition("-")
    if not first.isdigit() or (rest and not rest.isdigit()):
        warnings.append(f"Service '{service}': could not parse {what} '{value}'.")
        return None
    port = int(first)
    if not 1 <= port <= 65535:
        warnings.append(f"Service '{service}': {what} '{value}' is outside 1-65535.")
        return None
    if rest:
        warnings.append(
            f"Service '{service}': {what} range '{value}' was reduced to its first port."
        )
    return port


def _parse_ports(value: Any, warnings: list[str], service: str) -> list[PortMapping]:
    ports: list[PortMapping] = []
    for item in value or []:
        if isinstance(item, dict):  # long syntax
            if item.get("target") is None:
                continue
            container = _port_number(item["target"], warnings, service, "port target")
            if container is None:
                continue
            published = item.get("published")
            host = (
                _port_number(published, warnings, service, "published port")
                if published not in (None, "")
                else None
            )
            ports.append(
                PortMapping(
                    container_port=container,
                    host_port=host,
                    protocol=str(item.get("protocol") or "tcp").lower(),
                )
            )
            continue
        match = _PORT_RE.match(str(item).strip())
        if not match:
            warnings.append(f"Service '{service}': could not parse port mapping '{item}'.")
            continue
        container = _port_number(match.group("container"), warnings, service, "port mapping")
        if container is None:
            continue
        host = match.group("host")
        ports.append(
            PortMapping(
                container_port=container,
                host_port=_port_number(host, warnings, service, "published port") if host else None,
                protocol=(match.group("proto") or "tcp").lower(),
            )
        )
    return ports


def _parse_volume(item: Any, warnings: list[str], service: str) -> VolumeMount | None:
    if isinstance(item, dict):  # long syntax
        target = item.get("target")
        if not target:
            return None
        vol_type = item.get("type", "volume")
        source = item.get("source")
        if vol_type == "tmpfs":
            size = (item.get("tmpfs") or {}).get("size")
            return VolumeMount(target=str(target), kind="tmpfs", size=_normalise_memory(size))
        if vol_type == "bind":
            _warn_bind(source, warnings, service)
            kind = "bind"
        elif vol_type == "volume":
            kind = "named" if source else "anonymous"
        else:
            warnings.append(
                f"Service '{service}': volume type '{vol_type}' at '{target}' is not supported "
                "and was skipped."
            )
            return None
        return VolumeMount(
            target=str(target),
            source=str(source) if source else None,
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
        _warn_bind(source, warnings, service)
    return VolumeMount(
        target=target,
        source=source,
        kind="bind" if is_bind else "named",
        read_only=read_only,
    )


def _warn_bind(source: Any, warnings: list[str], service: str) -> None:
    warnings.append(
        f"Service '{service}': bind mount '{source}' has no direct Kubernetes "
        "equivalent and needs a storage strategy."
    )


def _parse_tmpfs(value: Any) -> list[VolumeMount]:
    """Service-level `tmpfs: /run` or `- /run:rw,size=64m`."""
    mounts: list[VolumeMount] = []
    for entry in _as_list(value):
        target, _, options = entry.partition(":")
        size = next(
            (opt.split("=", 1)[1] for opt in options.split(",") if opt.startswith("size=")), None
        )
        mounts.append(VolumeMount(target=target, kind="tmpfs", size=_normalise_memory(size)))
    return mounts


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
    test = value.get("test")
    test_list = [str(t) for t in test] if isinstance(test, list) else (["CMD-SHELL", str(test)] if test else [])
    if value.get("disable") or (test_list and test_list[0].upper() == "NONE"):
        return HealthCheck(disabled=True)
    return HealthCheck(
        test=test_list,
        interval_seconds=_duration_seconds(value.get("interval")),
        timeout_seconds=_duration_seconds(value.get("timeout")),
        retries=_optional_int(value.get("retries")),
        start_period_seconds=_duration_seconds(value.get("start_period")),
    )


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


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


def _parse_replicas(deploy: Any, warnings: list[str], service: str) -> int | None:
    if not isinstance(deploy, dict) or deploy.get("replicas") is None:
        return None
    replicas = _optional_int(deploy["replicas"])
    if replicas is None or replicas < 0:
        warnings.append(f"Service '{service}': ignoring invalid replicas '{deploy['replicas']}'.")
        return None
    return replicas


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
    '.env' is what Compose actually uses, so it wins over .env.example and
    friends, which only fill in keys it does not define.
    """
    root = resolve_project_path(path)
    base_dir = root if root.is_dir() else root.parent
    logger.info("Inspecting environment: %s", base_dir)

    variables: dict[str, EnvVarInfo] = {}
    files: list[str] = []
    warnings: list[str] = []

    env_paths = sorted(
        (p for p in base_dir.glob(".env*") if p.is_file()),
        key=lambda p: (p.name != ".env", p.name),
    )
    for env_path in env_paths:
        files.append(env_path.name)
        if env_path.name == ".env":
            warnings.append(
                "A '.env' file was found. Make sure it is git-ignored; its secret values are "
                "not returned by this tool."
            )
        for key, value in iter_env_file(env_path):
            if key in variables:
                continue
            secret = classify_env(key, value)
            variables[key] = EnvVarInfo(
                key=key,
                is_secret=secret,
                source=env_path.name,
                has_value=value != "",
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


def iter_env_file(path: Path) -> Iterator[tuple[str, str]]:
    """Stream KEY=value pairs from a dotenv file, with optional 'export' and quotes.

    A generator, so a caller that discards secret values never holds the whole
    file's worth of them.
    """
    with path.open(encoding="utf-8", errors="replace") as stream:
        for raw in stream:
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
                yield key, value


def _sanitize_name(name: str) -> str:
    """Turn a directory name into a DNS-1123 label (max 63 characters)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (slug or "app")[:63].rstrip("-")


__all__ = [
    "Interpolator",
    "find_compose_file",
    "find_dockerfile",
    "inspect_compose",
    "inspect_dockerfile",
    "inspect_environment",
    "inspect_project",
    "is_secret_key",
    "iter_env_file",
]

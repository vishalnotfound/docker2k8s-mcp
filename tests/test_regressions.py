"""Regression tests for bugs found in review: one test per failure mode."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from docker2k8s_mcp import config
from docker2k8s_mcp.errors import PathNotAllowed
from docker2k8s_mcp.generators import kubernetes as generator
from docker2k8s_mcp.security import REQUIRED_SECRET, classify_env, is_secret_key
from docker2k8s_mcp.services import docker_service, migration_service
from docker2k8s_mcp.validators import kubernetes as validator


def make_project(root: Path, compose: str, **files: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "docker-compose.yml").write_text(compose, encoding="utf-8")
    for name, body in files.items():
        (root / name.replace("__", ".")).write_text(body, encoding="utf-8")
    return root


def generate(root: Path, **kwargs) -> dict[str, dict]:
    result = generator.generate_manifests(migration_service.create_migration_plan(str(root)), **kwargs)
    docs = {}
    for manifest in result.manifests:
        doc = yaml.safe_load(Path(manifest.path).read_text(encoding="utf-8"))
        docs[f"{doc['kind']}/{doc['metadata']['name']}"] = doc
    return docs


def container(docs: dict[str, dict], ref: str) -> dict:
    return docs[ref]["spec"]["template"]["spec"]["containers"][0]


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key", ["DB_PASS", "SMTP_PASS", "STRIPE_KEY", "APP_KEY", "SENTRY_DSN", "dbPassword", "BASIC_AUTH"]
)
def test_common_credential_names_are_secret(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize(
    "key", ["PWD", "AUTHOR", "LOG_LEVEL", "BYPASS_CACHE", "STRIPE_PUBLISHABLE_KEY", "SSH_KEY_FILE"]
)
def test_bland_names_are_not_secret(key: str) -> None:
    assert not is_secret_key(key)


def test_url_with_empty_username_is_secret() -> None:
    assert classify_env("REDIS_URL", "redis://:hunter2@redis:6379/0")
    assert not classify_env("API_URL", "http://api:8000/v1")


def test_credentials_never_reach_a_configmap(sandbox: Path) -> None:
    root = make_project(
        sandbox / "leak",
        "services:\n  api:\n    image: api:1.0\n    environment:\n"
        "      DB_PASS: ${DB_PASS}\n      REDIS_URL: ${REDIS_URL}\n      LOG_PREFIX: x\n",
        __env="DB_PASS=hunter2\nREDIS_URL=redis://:hunter2@redis:6379/0\n",
    )
    docs = generate(root)
    text = yaml.safe_dump(docs)
    assert "hunter2" not in text
    assert set(docs["Secret/api-secret"]["stringData"]) == {"DB_PASS", "REDIS_URL"}


def test_bland_key_referencing_secret_variable_becomes_secret(sandbox: Path) -> None:
    root = make_project(
        sandbox / "secret-ref",
        "services:\n  api:\n    image: api:1.0\n    environment:\n      DATABASE_URL: ${DATABASE_URL}\n",
        __env="DATABASE_URL=postgres://u:p@db/app\n",
    )
    plan = migration_service.create_migration_plan(str(root))

    assert plan.secrets == ["DATABASE_URL"]
    # It is defined, just secret: no misleading "undefined variable" warning.
    assert not any(w.code == "UNRESOLVED_INTERPOLATION" for w in plan.warnings)


# --------------------------------------------------------------------------
# Compose parsing
# --------------------------------------------------------------------------
def test_compose_uses_yaml_1_2_scalars(sandbox: Path) -> None:
    root = make_project(
        sandbox / "yaml12",
        "services:\n  web:\n    image: nginx:1.27\n    restart: no\n    ports:\n      - 22:22\n"
        "    environment:\n      FEATURE: yes\n      DEBUG: false\n",
    )
    web = docker_service.inspect_compose(str(root)).services[0]

    assert web.restart == "no"  # YAML 1.1 would make this False and crash
    assert web.ports[0].container_port == 22  # not base-60 1342
    assert web.environment == {"FEATURE": "yes", "DEBUG": "false"}


def test_interpolation_covers_image_and_ports(sandbox: Path) -> None:
    root = make_project(
        sandbox / "interp",
        "name: shop\nservices:\n  api:\n    image: ghcr.io/acme/api:${TAG:-1.2.3}\n"
        "    ports:\n      - \"${API_PORT:-8000}:8000\"\n    command: sh -c 'echo $$HOME'\n",
    )
    compose = docker_service.inspect_compose(str(root))
    api = compose.services[0]

    assert compose.project_name == "shop"  # top-level name: wins over the directory
    assert api.image == "ghcr.io/acme/api:1.2.3"
    assert [(p.host_port, p.container_port) for p in api.ports] == [(8000, 8000)]
    assert api.command == ["sh", "-c", "echo $HOME"]


def test_secret_variable_is_not_substituted_outside_environment(sandbox: Path) -> None:
    root = make_project(
        sandbox / "secret-cmd",
        "services:\n  api:\n    image: api:1.0\n    command: [\"run\", \"--key=${API_TOKEN}\"]\n",
        __env="API_TOKEN=tok-123\n",
    )
    compose = docker_service.inspect_compose(str(root))

    assert compose.services[0].command == ["run", "--key=${API_TOKEN}"]
    assert any("API_TOKEN" in w for w in compose.warnings)


def test_build_mapping_without_context_defaults_to_project_dir(sandbox: Path) -> None:
    root = make_project(
        sandbox / "noctx",
        "services:\n  api:\n    build:\n      dockerfile: Dockerfile.dev\n",
        Dockerfile__dev="FROM python:3.12\nEXPOSE 9000\n",
    )
    plan = migration_service.create_migration_plan(str(root))

    assert plan.blockers == []
    assert plan.services[0].build_required is True
    assert [p.container_port for p in plan.services[0].container_ports] == [9000]


def test_companion_images_are_not_datastores(sandbox: Path) -> None:
    root = make_project(
        sandbox / "companions",
        "services:\n  ui:\n    image: mongo-express:1.0\n  cmd:\n    image: rediscommander/redis-commander:latest\n"
        "  db:\n    image: bitnami/postgresql:16\n",
    )
    stateful = {s.name: s.stateful for s in docker_service.inspect_compose(str(root)).services}

    assert stateful == {"ui": False, "cmd": False, "db": True}


def test_long_syntax_port_range_does_not_crash(sandbox: Path) -> None:
    root = make_project(
        sandbox / "range",
        "services:\n  api:\n    image: api:1.0\n    ports:\n      - target: 8000\n        published: \"8000-8001\"\n",
    )
    compose = docker_service.inspect_compose(str(root))

    assert compose.services[0].ports[0].host_port == 8000
    assert any("range" in w for w in compose.warnings)


def test_untranslated_keys_and_override_files_are_reported(sandbox: Path) -> None:
    root = make_project(
        sandbox / "extras",
        "services:\n  api:\n    image: api:1.0\n    profiles: [debug]\n    ulimits: {nofile: 1024}\n"
        "secrets:\n  token:\n    file: ./token\n",
    )
    (root / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    warnings = " ".join(docker_service.inspect_compose(str(root)).warnings)

    assert "profile" in warnings and "ulimits" in warnings
    assert "Top-level 'secrets'" in warnings
    assert "docker-compose.override.yml" in warnings


def test_environment_prefers_dot_env_over_example(sandbox: Path) -> None:
    root = make_project(
        sandbox / "envs",
        "services:\n  a:\n    image: a:1\n",
        __env="LOG_LEVEL=DEBUG\n",
        __env__example="LOG_LEVEL=\nEXTRA=1\n",
    )
    variables = {v.key: v for v in docker_service.inspect_environment(str(root)).variables}

    assert (variables["LOG_LEVEL"].source, variables["LOG_LEVEL"].value) == (".env", "DEBUG")
    assert variables["EXTRA"].source == ".env.example"


# --------------------------------------------------------------------------
# Dockerfile parsing
# --------------------------------------------------------------------------
def test_dockerfile_platform_flag_and_continuation_comments(sandbox: Path) -> None:
    path = sandbox / "Dockerfile"
    path.write_text(
        "FROM --platform=$BUILDPLATFORM golang:1.23 AS build\n"
        "FROM alpine\n"
        "ENV A=1 \\\n"
        "    # a comment inside the continuation\n"
        "    B=2\n"
        "EXPOSE\t8080\n",
        encoding="utf-8",
    )
    result = docker_service.inspect_dockerfile(str(path))

    assert [(s.base_image, s.name) for s in result.stages] == [("golang:1.23", "build"), ("alpine", None)]
    assert result.env_keys == ["A", "B"]
    assert result.exposed_ports == [8080]
    assert any("alpine" in note for note in result.notes)  # untagged means :latest


def test_lowercase_dockerfile_is_found(sandbox: Path) -> None:
    root = sandbox / "lower"
    root.mkdir()
    (root / "dockerfile").write_text("FROM alpine:3.20\nEXPOSE 8000\n", encoding="utf-8")

    assert docker_service.inspect_project(str(root)).dockerfile_paths == ["dockerfile"]
    assert docker_service.inspect_dockerfile(str(root)).exposed_ports == [8000]


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def test_compose_file_path_plans_like_its_directory(sandbox: Path) -> None:
    root = make_project(
        sandbox / "by-file",
        "services:\n  api:\n    build: .\n    environment:\n      LOG_LEVEL: ${LOG_LEVEL}\n",
        __env="LOG_LEVEL=DEBUG\n",
        Dockerfile="FROM alpine:3.20\nEXPOSE 7000\n",
    )
    by_file = migration_service.create_migration_plan(str(root / "docker-compose.yml"))

    assert by_file.project_path == str(root)
    assert by_file.services[0].env[0].value == "DEBUG"
    assert by_file.services[0].container_ports[0].container_port == 7000


def test_port_names_are_unique(sandbox: Path) -> None:
    root = make_project(
        sandbox / "traefik",
        "services:\n  proxy:\n    image: traefik:v3.1\n    ports: [\"80:80\", \"8080:8080\", \"53:53/udp\", \"53:53\"]\n",
    )
    docs = generate(root)
    names = [p["name"] for p in docs["Service/proxy"]["spec"]["ports"]]

    assert len(names) == len(set(names)) == 4
    assert validator.validate_manifests(str(root.parent / "generated" / "traefik" / "k8s")).valid


def test_web_app_with_volume_is_a_reachable_deployment(sandbox: Path) -> None:
    root = make_project(
        sandbox / "wordpress",
        "services:\n  wordpress:\n    image: wordpress:6\n    ports: [\"8080:80\"]\n"
        "    volumes:\n      - wp-data:/var/www/html\nvolumes:\n  wp-data:\n",
    )
    docs = generate(root)
    deployment = docs["Deployment/wordpress"]

    assert docs["Service/wordpress"]["spec"]["type"] == "NodePort"
    assert "clusterIP" not in docs["Service/wordpress"]["spec"]
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert "PersistentVolumeClaim/wordpress-wp-data" in docs


def test_shared_volume_becomes_one_claim(sandbox: Path) -> None:
    root = make_project(
        sandbox / "shared",
        "services:\n  api:\n    image: api:1\n    volumes: [\"uploads:/data\"]\n"
        "  worker:\n    image: worker:1\n    volumes: [\"uploads:/data\"]\nvolumes:\n  uploads:\n",
    )
    docs = generate(root)
    claims = [k for k in docs if k.startswith("PersistentVolumeClaim/")]

    assert claims == ["PersistentVolumeClaim/shared-uploads"]
    for ref in ("Deployment/api", "Deployment/worker"):
        volumes = docs[ref]["spec"]["template"]["spec"]["volumes"]
        assert volumes[0]["persistentVolumeClaim"]["claimName"] == "shared-uploads"


def test_tmpfs_becomes_memory_empty_dir(sandbox: Path) -> None:
    root = make_project(
        sandbox / "tmpfs",
        "services:\n  api:\n    image: api:1.0\n    tmpfs: /run:size=64m\n    volumes:\n"
        "      - type: tmpfs\n        target: /tmp/cache\n",
    )
    volumes = generate(root)["Deployment/api"]["spec"]["template"]["spec"]["volumes"]

    assert {"name": "tmpfs-run", "emptyDir": {"medium": "Memory", "sizeLimit": "64Mi"}} in volumes
    assert {"name": "tmpfs-cache", "emptyDir": {"medium": "Memory"}} in volumes


def test_liveness_does_not_depend_on_the_http_healthcheck(project: Path) -> None:
    api = migration_service.create_migration_plan(str(project)).services[0]

    assert api.readiness_probe is not None and api.readiness_probe.kind == "httpGet"
    # /health often checks the database; liveness only checks the process is listening.
    assert api.liveness_probe is not None and api.liveness_probe.kind == "tcpSocket"
    assert api.liveness_probe.port == 8000


def test_url_without_port_probes_the_scheme_default(sandbox: Path) -> None:
    root = make_project(
        sandbox / "nginx",
        "services:\n  web:\n    image: nginx:1.27\n    ports: [\"8080:8080\"]\n    healthcheck:\n"
        "      test: [\"CMD\", \"curl\", \"-f\", \"https://localhost/healthz\"]\n",
    )
    web = migration_service.create_migration_plan(str(root)).services[0]

    assert (web.readiness_probe.port, web.readiness_probe.scheme) == (443, "HTTPS")
    assert 443 in [p.container_port for p in web.container_ports]


def test_service_without_healthcheck_gets_tcp_readiness(sandbox: Path) -> None:
    root = make_project(sandbox / "nohc", "services:\n  api:\n    image: api:1.0\n    expose: [\"9000\"]\n")
    api = migration_service.create_migration_plan(str(root)).services[0]

    assert api.readiness_probe is not None and api.readiness_probe.kind == "tcpSocket"
    assert api.liveness_probe is None


def test_blockers_reach_the_plan_and_security_context(sandbox: Path) -> None:
    root = make_project(
        sandbox / "priv",
        "services:\n  agent:\n    image: agent:1\n    privileged: true\n    user: \"1000\"\n"
        "    cap_add: [CAP_NET_ADMIN]\n",
    )
    plan = migration_service.create_migration_plan(str(root))
    context = generate(root)["Deployment/agent"]["spec"]["template"]["spec"]["containers"][0]["securityContext"]

    assert any("privileged" in b for b in plan.blockers)
    assert context["privileged"] is True
    assert context["runAsUser"] == 1000
    assert context["capabilities"] == {"add": ["NET_ADMIN"]}


def test_named_dockerfile_user_does_not_force_run_as_non_root(project: Path) -> None:
    # The kubelet cannot verify a named user, so runAsNonRoot would block the pod.
    api = migration_service.create_migration_plan(str(project)).services[0]

    assert api.run_as_non_root is False
    assert api.allow_privilege_escalation is False


def test_services_without_resources_get_default_requests(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))
    db = plan.services[1]

    assert db.resources is not None and db.resources.memory_request and not db.resources.memory_limit
    assert any(w.code == "DEFAULT_REQUESTS" for w in plan.warnings)


def test_replicas_zero_is_respected(sandbox: Path) -> None:
    root = make_project(sandbox / "zero", "services:\n  api:\n    image: api:1\n    deploy:\n      replicas: 0\n")

    assert migration_service.create_migration_plan(str(root)).services[0].replicas == 0


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def test_output_dir_is_sandboxed(project: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    plan = migration_service.create_migration_plan(str(project))
    outside = tmp_path_factory.mktemp("outside")

    with pytest.raises(PathNotAllowed):
        generator.generate_manifests(plan, output_dir=str(outside / "k8s"))


def test_relative_output_dir_lands_in_generated_dir(project: Path, sandbox: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))
    result = generator.generate_manifests(plan, output_dir="custom")

    assert Path(result.output_dir) == (sandbox / "generated" / "custom").resolve()


def test_regeneration_sets_stale_files_aside(sandbox: Path) -> None:
    root = make_project(
        sandbox / "stale", "services:\n  api:\n    image: api:1\n    environment:\n      API_TOKEN: x\n"
    )
    first = generator.generate_manifests(migration_service.create_migration_plan(str(root)))
    out = Path(first.output_dir)
    (out / "notes.yaml").write_text("# my own file\n", encoding="utf-8")
    (root / "docker-compose.yml").write_text("services:\n  api:\n    image: api:1\n", encoding="utf-8")

    second = generator.generate_manifests(migration_service.create_migration_plan(str(root)))

    assert sorted(p.name for p in out.glob("*.yaml")) == ["api-deployment.yaml", "notes.yaml"]
    assert (out / "api-secret.yaml.stale").exists()  # set aside, not deleted
    assert any("stale" in w for w in second.warnings)


def test_regeneration_keeps_filled_in_secret_values(project: Path) -> None:
    plan = migration_service.create_migration_plan(str(project))
    out = Path(generator.generate_manifests(plan).output_dir)
    secret_file = out / "api-secret.yaml"
    secret_file.write_text(
        secret_file.read_text(encoding="utf-8").replace(REQUIRED_SECRET, "filled-by-human"),
        encoding="utf-8",
    )

    result = generator.generate_manifests(plan)

    assert yaml.safe_load(secret_file.read_text(encoding="utf-8"))["stringData"]["DB_PASSWORD"] == "filled-by-human"
    assert "filled-by-human" not in result.model_dump_json()  # never returned to the caller


def test_long_names_produce_valid_labels(sandbox: Path) -> None:
    root = make_project(
        sandbox / ("a-very-long-company-project-directory-name-for-" + "x" * 20),
        "services:\n  api-gateway-service:\n    image: api:1\n    ports: [\"8000:8000\"]\n",
    )
    generate(root)
    out = root.parent / "generated" / root.name[:63].rstrip("-") / "k8s"

    assert validator.validate_manifests(str(out)).valid


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def write_manifest(sandbox: Path, text: str) -> str:
    path = sandbox / "m.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_validation_catches_duplicate_port_names(sandbox: Path) -> None:
    path = write_manifest(
        sandbox,
        "apiVersion: v1\nkind: Service\nmetadata: {name: web}\nspec:\n  ports:\n"
        "  - {name: http, port: 80}\n  - {name: http, port: 8080}\n",
    )
    codes = {i.code for i in validator.validate_manifests(path).issues}

    assert "DUPLICATE_PORT_NAME" in codes


def test_validation_applies_dns1035_to_services(sandbox: Path) -> None:
    path = write_manifest(sandbox, "apiVersion: v1\nkind: Service\nmetadata: {name: 2fa}\nspec:\n  ports: [{port: 80}]\n")

    assert any(i.code == "INVALID_NAME" for i in validator.validate_manifests(path).issues)


def test_validation_catches_long_label_values(sandbox: Path) -> None:
    path = write_manifest(
        sandbox, f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  labels: {{app: {'x' * 64}}}\n"
    )

    assert any(i.code == "INVALID_LABEL_VALUE" for i in validator.validate_manifests(path).issues)


def test_validation_finds_base64_placeholders(sandbox: Path) -> None:
    path = write_manifest(
        sandbox, "apiVersion: v1\nkind: Secret\nmetadata: {name: s}\ndata:\n  K: PFJFUVVJUkVEX1NFQ1JFVD4=\n"
    )

    assert any(i.code == "PLACEHOLDER_SECRET" for i in validator.validate_manifests(path).issues)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def test_home_or_root_working_directory_is_not_sandboxed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOWED_ROOTS", raising=False)
    monkeypatch.setenv("ALLOWED_ROOTS", "")
    monkeypatch.chdir(Path.home())
    config.get_settings.cache_clear()

    roots = config.get_settings().allowed_root_paths

    assert Path.home().resolve() not in roots
    assert Path(Path.home().anchor) not in roots


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    config.get_settings.cache_clear()

    with pytest.raises(ValueError):
        config.get_settings()

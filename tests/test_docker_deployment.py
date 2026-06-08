"""Deployment contract tests for the supported Docker Compose layouts."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    """Return one deployment file as UTF-8 text for contract assertions."""
    return (ROOT / path).read_text(encoding="utf-8")


def test_primary_compose_has_two_versioned_services_and_shared_state() -> None:
    """Default Compose must expose the application processes and automatic backup."""
    compose = _read("compose.yaml")

    assert "  bridge:" in compose
    assert "  fedify-gateway:" in compose
    assert "  nginx:" not in compose
    assert compose.count("${BRIDGE_VERSION:?") == 3
    assert compose.count("DATABASE_URL: sqlite:////data/bridge.db") == 1
    assert compose.count("- bridge-data:/data") == 2
    assert '127.0.0.1:${BRIDGE_PUBLISHED_PORT:-8080}:8080' in compose
    assert '127.0.0.1:${GATEWAY_PUBLISHED_PORT:-3000}:3000' in compose
    assert "condition: service_healthy" in compose
    assert "BRIDGE_GATEWAY_URL: http://fedify-gateway:3000" in compose
    assert "BRIDGE_EVENTS_URL: http://bridge:8080/internal/activitypub/events" in compose
    gateway_block = compose.split("  fedify-gateway:", 1)[1].split("\n  backup:", 1)[0]
    assert "DATABASE_URL" not in gateway_block
    assert "bridge-data:/data" not in gateway_block
    assert "BRIDGE_EVENTS_URL: http://bridge:8080/internal/activitypub/events" in compose
    assert "fedify-gateway/.env" not in compose
    assert "  backup:" in compose
    assert "python\n      - -m\n      - src.db.backup\n      - serve" in compose
    assert "${BACKUP_HOST_DIR:-./backups}:/backups" in compose
    assert "${BACKUP_INTERVAL_SECONDS:-86400}" in compose
    assert "${BACKUP_RETENTION_COUNT:-14}" in compose
    # The dedicated network isolates service discovery without blocking required outbound traffic.
    assert "bridge-internal: {}" in compose
    assert "internal: true" not in compose


def test_build_override_builds_both_images_with_shared_version() -> None:
    """Local builds must stamp both images with the selected release version."""
    compose = _read("compose.build.yaml")

    assert compose.count("APP_VERSION: ${BRIDGE_VERSION:?") == 2
    assert "dockerfile: Dockerfile" in compose
    assert "dockerfile: fedify-gateway/Dockerfile" in compose


def test_optional_nginx_routes_gateway_paths_and_bridge_fallback() -> None:
    """Bundled nginx must preserve the project's one-origin route ownership."""
    override = _read("compose.nginx.yaml")
    config = _read("deploy/nginx/default.conf.template")

    assert "  nginx:" in override
    for route in (
        "/.well-known/webfinger",
        "/.well-known/discord-fediverse-bridge/",
        "/actors/",
        "/c/",
        "/users/",
        "/communities/",
        "/inbox",
        "/activitypub/",
    ):
        assert route in config
    assert "proxy_pass http://fedify-gateway:3000" in config
    assert "location / { proxy_pass http://bridge:8080" in config


def test_dockerfiles_use_non_root_users_version_and_exec_commands() -> None:
    """Both runtime images must be reproducible and avoid root execution."""
    python_dockerfile = _read("Dockerfile")
    gateway_dockerfile = _read("fedify-gateway/Dockerfile")

    assert "FROM python:3.12-slim" in python_dockerfile
    assert "COPY pyproject.toml VERSION" in python_dockerfile
    assert "USER bridge" in python_dockerfile
    assert 'CMD ["python", "-m", "src.app"]' in python_dockerfile
    assert "EXPOSE 8080" in python_dockerfile

    assert "FROM node:22-bookworm-slim" in gateway_dockerfile
    assert "COPY VERSION ./VERSION" in gateway_dockerfile
    assert "RUN npm ci" in gateway_dockerfile
    assert "RUN npm run check" in gateway_dockerfile
    assert "USER bridge" in gateway_dockerfile
    assert 'CMD ["node", "./node_modules/tsx/dist/cli.mjs", "src/server.ts"]' in gateway_dockerfile
    assert "EXPOSE 3000" in gateway_dockerfile


def test_dockerignore_excludes_local_secrets_and_generated_state() -> None:
    """Build contexts must not copy local credentials or mutable state."""
    ignored = _read(".dockerignore").splitlines()

    for entry in (".env", ".venv", "node_modules", "*.db", "plans"):
        assert entry in ignored
    assert "VERSION" not in ignored
    assert "web" not in ignored
    assert "vendor" not in ignored


def test_deployment_uses_one_root_env_file() -> None:
    """All supported launch paths must read the same root .env file."""
    compose = _read("compose.yaml")
    package = _read("fedify-gateway/package.json")
    systemd = _read("systemd-services.sh")
    env_example = _read(".env.example")

    assert compose.count("      - ${COMPOSE_ENV_FILE:-.env}") == 3
    assert "BRIDGE_ENV_FILE" not in compose
    assert "GATEWAY_ENV_FILE" not in compose
    assert "--env-file=../.env --env-file=.env" not in package
    assert "--env-file=../.env" in package
    assert 'BRIDGE_ENV_FILE="${BRIDGE_ENV_FILE:-$PROJECT_DIR/.env}"' in systemd
    assert systemd.count('EnvironmentFile=$BRIDGE_ENV_FILE') == 2
    assert "# Docker Compose settings." in env_example
    assert "FEDIFY_BRIDGE_PRIVATE_KEY_JWK_JSON=" not in env_example
    assert "BACKUP_HOST_DIR=./backups" in env_example
    assert not (ROOT / ".env.docker.example").exists()
    assert not (ROOT / "fedify-gateway/.env.example").exists()


def test_environment_example_uses_one_public_url_and_no_derived_endpoints() -> None:
    """Operators must configure one public URL instead of repeated equivalent endpoints."""
    env_example = _read(".env.example")

    assert "PUBLIC_BASE_URL=https://discord-bridge.example.com" in env_example
    for expected in (
        "BRIDGE_BIND_HOST=127.0.0.1",
        "BRIDGE_BIND_PORT=8081",
        "GATEWAY_BIND_PORT=3000",
        "BRIDGE_GATEWAY_URL=http://127.0.0.1:3000",
        "BRIDGE_PUBLISHED_PORT=8081",
        "GATEWAY_PUBLISHED_PORT=3000",
    ):
        assert expected in env_example
    for obsolete in (
        "FEDIFY_ORIGIN=",
        "PUBLIC_BRIDGE_BASE_URL=",
        "DISCORD_OAUTH_REDIRECT_URI=",
        "PUBLIC_DOMAIN=",
        "GATEWAY_UPSTREAM=",
        "PYTHON_BRIDGE_UPSTREAM=",
        "FEDIFY_GATEWAY_URL=",
        "PYTHON_BRIDGE_EVENTS_URL=",
        "BRIDGE_HOST_PORT=",
        "GATEWAY_HOST_PORT=",
        "FEDIFY_PORT=",
        "INTERNAL_HTTP_HOST=",
        "INTERNAL_HTTP_PORT=",
    ):
        assert obsolete not in env_example


def test_prod_and_dev_environment_examples_are_isolated() -> None:
    """Prod and dev examples must select distinct Compose projects, env files, ports, and origins."""
    prod = _read(".env.example")
    dev = _read(".env.dev.example")

    assert "COMPOSE_PROJECT_NAME=discord-bridge-prod" in prod
    assert "COMPOSE_ENV_FILE=.env" in prod
    assert "PUBLIC_BASE_URL=https://discord-bridge.example.com" in prod
    assert "BRIDGE_PUBLISHED_PORT=8081" in prod
    assert "GATEWAY_PUBLISHED_PORT=3000" in prod

    assert "COMPOSE_PROJECT_NAME=discord-bridge-dev" in dev
    assert "COMPOSE_ENV_FILE=.env.dev" in dev
    assert "PUBLIC_BASE_URL=https://discord-bridge-dev.example.com" in dev
    assert "BRIDGE_PUBLISHED_PORT=8181" in dev
    assert "GATEWAY_PUBLISHED_PORT=3100" in dev
    assert "BRIDGE_VERSION=dev" in dev


def test_release_workflow_publishes_both_images_from_version_tags() -> None:
    """Release tags must publish matched bridge/gateway images and create a GitHub Release."""
    workflow = _read(".github/workflows/release.yml")

    assert 'tags: ["v*.*.*"]' in workflow
    assert "packages: write" in workflow
    assert "contents: write" in workflow
    assert "docker/login-action@v3" in workflow
    assert "docker/build-push-action@v6" in workflow
    assert "discord-fediverse-bridge-gateway" in workflow
    assert "gh release create" in workflow
    assert "VERSION" in workflow


def test_nginx_setup_accepts_env_file_and_instance_name_flags() -> None:
    """One nginx setup script must install independent prod and dev configurations."""
    script = _read("fedify-gateway/nginx-setup.sh")

    assert "--env-file" in script
    assert "--name" in script
    assert 'discord-fediverse-bridge-${INSTANCE_NAME}' in script

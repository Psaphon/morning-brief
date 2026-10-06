"""Tests for Docker deployment configuration.

Validates Dockerfile structure, docker-compose.yml settings, and security
constraints without requiring a running Docker daemon.
"""

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_dockerfile() -> str:
    return (PROJECT_ROOT / "Dockerfile").read_text()


def _load_compose() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())


# ── Dockerfile tests ─────────────────────────────────────────────────────────


class TestDockerfile:
    def test_multi_stage_build(self):
        content = _read_dockerfile()
        assert content.count("FROM ") >= 2, "Dockerfile should use multi-stage build"

    def test_builder_stage_exists(self):
        content = _read_dockerfile()
        assert "AS builder" in content

    def test_runtime_stage_uses_slim(self):
        content = _read_dockerfile()
        assert "python:3.11-slim AS runtime" in content

    def test_runs_as_non_root(self):
        content = _read_dockerfile()
        assert "USER app" in content
        assert "useradd" in content

    def test_code_world_readable(self):
        # Rootless runs as uid 0 with cap_drop ALL; 0600 files from a 077 umask
        # host (hub) were unreadable and the pipeline died on import.
        assert "chmod -R a+rX /app" in _read_dockerfile()

    def test_healthcheck_defined(self):
        content = _read_dockerfile()
        assert "HEALTHCHECK" in content

    def test_copies_application_code(self):
        content = _read_dockerfile()
        assert "COPY src/ src/" in content
        assert "COPY templates/ templates/" in content

    def test_copies_test_infrastructure(self):
        content = _read_dockerfile()
        assert "COPY tests/ tests/" in content
        assert "COPY pyproject.toml" in content

    def test_copies_scripts(self):
        content = _read_dockerfile()
        assert "COPY scripts/ scripts/" in content

    def test_data_directory_created(self):
        content = _read_dockerfile()
        assert "mkdir -p data" in content


# ── docker-compose.yml tests ────────────────────────────────────────────────


class TestDockerCompose:
    def test_morning_brief_service_exists(self):
        compose = _load_compose()
        assert "morning-brief" in compose["services"]

    def test_ollama_not_in_compose(self):
        """Ollama runs on the host for GPU access, not in a container."""
        compose = _load_compose()
        assert "ollama" not in compose["services"]

    def test_data_volume_mounted(self):
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        assert any(v.endswith(":/app/data") for v in svc["volumes"])

    def test_data_volume_is_repo_relative(self):
        # An absolute host path pinned the mount to one machine's home (/home/comp),
        # so the pipeline wrote nowhere useful on any other host.
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        data = [v for v in svc["volumes"] if v.endswith(":/app/data")]
        assert data == ["./data:/app/data"]

    def test_env_file_loaded(self):
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        assert ".env" in svc["env_file"]

    def test_ollama_host_points_to_host(self):
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        env_list = svc.get("environment", [])
        ollama_vars = [e for e in env_list if "OLLAMA_HOST" in str(e)]
        assert ollama_vars, "OLLAMA_HOST must be set in environment"
        assert "host.docker.internal" in ollama_vars[0]

    def test_ollama_host_overridable_from_env(self):
        # `environment` wins over `env_file`, so a literal value here would silently
        # ignore OLLAMA_HOST in .env. It must interpolate with the host as default.
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        env_list = svc.get("environment", [])
        assert "OLLAMA_HOST=${OLLAMA_HOST:-http://host.docker.internal:11434}" in env_list

    def test_host_gateway_mapping(self):
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        extra_hosts = svc.get("extra_hosts", [])
        assert any("host.docker.internal" in h for h in extra_hosts)

    def test_security_cap_drop_all(self):
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        assert "ALL" in svc["cap_drop"]

    def test_security_no_new_privileges(self):
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        assert "no-new-privileges:true" in svc["security_opt"]

    def test_container_user_set_by_run_script(self):
        # scripts/run-pipeline.sh picks 0:0 under rootless Docker, else the host uid.
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        assert svc["user"] == "${MB_UID:-1000}:${MB_GID:-1000}"

    def test_container_never_deploys(self):
        # The host unit publishes; environment beats env_file, so .env can't re-enable it.
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        assert "DEPLOY_ENABLED=false" in svc["environment"]

    def test_restart_policy_no(self):
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        assert svc["restart"] == "no"


def test_signals_enabled_by_default_in_compose():
    # atrade's paper cycle silently falls back to baseline-only without signals
    env = _load_compose()["services"]["morning-brief"]["environment"]
    assert "SIGNALS_ENABLED=${SIGNALS_ENABLED:-true}" in env


def test_image_contains_the_ticker_map():
    # Without it, signal emission fails inside the container and atrade gets no
    # signals; the pipeline itself still succeeds (hub, 2026-10-03).
    assert "COPY config/ config/" in (PROJECT_ROOT / "Dockerfile").read_text()
    assert (PROJECT_ROOT / "config" / "ticker_map.toml").is_file()

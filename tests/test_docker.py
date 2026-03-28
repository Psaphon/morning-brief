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
        assert "./data:/app/data" in svc["volumes"]

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

    def test_restart_policy_no(self):
        compose = _load_compose()
        svc = compose["services"]["morning-brief"]
        assert svc["restart"] == "no"

"""Tests for scripts/run-pipeline.sh: the container user under rootless vs rootful Docker.

Runs the real script against a fake `docker` on PATH that reports the daemon's
security options and records how compose was invoked.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "run-pipeline.sh"

# A bash function, not a file on PATH: hub mounts /tmp noexec, and bash skips a
# PATH entry it can't execute, which once fell through to the real docker and ran
# the pipeline. Functions win over PATH, and PATH below holds no docker at all.
FAKE_DOCKER = """() {
    if [[ "$1" == info ]]; then
        [[ -n "${FAKE_DOCKER_INFO_FAILS:-}" ]] && return 1
        echo "$FAKE_SECURITY_OPTIONS"
        return 0
    fi
    echo "MB_UID=${MB_UID} MB_GID=${MB_GID} ARGS=$*"
    return "${FAKE_COMPOSE_EXIT:-0}"
}"""

TOOLS = ("bash", "dirname", "grep", "id")


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in TOOLS:
        link = bin_dir / tool
        if not link.exists():
            link.symlink_to(shutil.which(tool))
    full_env = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "BASH_FUNC_docker%%": FAKE_DOCKER,
        **env,
    }
    return subprocess.run(
        [str(bin_dir / "bash"), str(SCRIPT)],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def test_no_real_docker_reachable(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_SECURITY_OPTIONS="[]")
    assert "ARGS=compose up" in result.stdout, result.stderr


def test_rootless_runs_as_container_root(tmp_path: Path) -> None:
    # Under rootless, container uid 0 is the host user; the image's own user maps
    # to a subordinate uid that can't write data/ (proven on hub, 2026-09-29).
    out = _run(tmp_path, FAKE_SECURITY_OPTIONS='["name=seccomp","name=rootless"]').stdout
    assert "MB_UID=0 MB_GID=0" in out


def test_rootful_runs_as_host_user(tmp_path: Path) -> None:
    out = _run(tmp_path, FAKE_SECURITY_OPTIONS='["name=seccomp,profile=builtin"]').stdout
    assert f"MB_UID={os.getuid()} MB_GID={os.getgid()}" in out


def test_unreachable_daemon_falls_back_to_host_user(tmp_path: Path) -> None:
    out = _run(tmp_path, FAKE_DOCKER_INFO_FAILS="1").stdout
    assert f"MB_UID={os.getuid()} MB_GID={os.getgid()}" in out


def test_builds_and_returns_the_pipeline_exit_code(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_SECURITY_OPTIONS="[]", FAKE_COMPOSE_EXIT="3")
    assert result.returncode == 3
    assert "compose up --build --abort-on-container-exit --exit-code-from morning-brief" in (
        result.stdout
    )


@pytest.mark.parametrize("name", ["run-pipeline.sh", "install-user-units.sh"])
def test_scripts_are_executable(name: str) -> None:
    # systemd execs ExecStart directly; a missing +x bit fails the unit at 04:15.
    assert os.access(PROJECT_ROOT / "scripts" / name, os.X_OK)

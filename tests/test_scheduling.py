"""Tests for systemd scheduling configuration and health-check logic.

Validates the service/timer unit files and the post-run health-check script
without requiring a running systemd daemon.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_FILE = PROJECT_ROOT / "morning-brief.service"
TIMER_FILE = PROJECT_ROOT / "morning-brief.timer"
HEALTH_CHECK = PROJECT_ROOT / "scripts" / "health-check.sh"


# ── Service unit tests ────────────────────────────────────────────────────────


class TestServiceUnit:
    def _content(self) -> str:
        return SERVICE_FILE.read_text()

    def test_service_file_exists(self):
        assert SERVICE_FILE.exists(), "morning-brief.service must exist"

    def test_type_oneshot(self):
        assert "Type=oneshot" in self._content()

    def test_working_directory_uses_home(self):
        # %h expands to the user's home directory in systemd units
        assert "WorkingDirectory=%h" in self._content()

    def test_timezone_eastern(self):
        assert "TZ=America/New_York" in self._content()

    def test_exec_start_docker_compose(self):
        assert "docker compose up" in self._content()

    def test_health_check_called_on_success(self):
        # ExecStartPost runs the health-check after the main command succeeds
        assert "ExecStartPost=" in self._content()
        assert "health-check.sh" in self._content()

    def test_output_to_journal(self):
        assert "StandardOutput=journal" in self._content()
        assert "StandardError=journal" in self._content()

    def test_install_section_present(self):
        assert "[Install]" in self._content()
        assert "WantedBy=default.target" in self._content()


# ── Timer unit tests ──────────────────────────────────────────────────────────


class TestTimerUnit:
    def _content(self) -> str:
        return TIMER_FILE.read_text()

    def test_timer_file_exists(self):
        assert TIMER_FILE.exists(), "morning-brief.timer must exist"

    def test_fires_at_0415(self):
        assert "04:15:00" in self._content()

    def test_timezone_eastern(self):
        assert "TimeZone=America/New_York" in self._content()

    def test_persistent_enabled(self):
        # Re-fires after missed runs (e.g. system was off)
        assert "Persistent=true" in self._content()

    def test_install_section_present(self):
        assert "[Install]" in self._content()
        assert "WantedBy=timers.target" in self._content()


# ── Health-check script tests ─────────────────────────────────────────────────


class TestHealthCheckScript:
    def test_script_exists(self):
        assert HEALTH_CHECK.exists(), "scripts/health-check.sh must exist"

    def test_script_is_executable(self):
        assert os.access(HEALTH_CHECK, os.X_OK), "scripts/health-check.sh must be executable"

    def test_script_has_shebang(self):
        first_line = HEALTH_CHECK.read_text().splitlines()[0]
        assert first_line.startswith("#!/"), "script must have a shebang"

    def test_script_uses_set_euo_pipefail(self):
        content = HEALTH_CHECK.read_text()
        assert "set -euo pipefail" in content

    def test_max_age_is_one_hour(self):
        content = HEALTH_CHECK.read_text()
        assert "MAX_AGE_SECONDS=3600" in content

    def test_failure_log_path(self):
        content = HEALTH_CHECK.read_text()
        assert "morning-brief/failures.log" in content

    def test_passes_with_fresh_dashboard(self, tmp_path):
        """Health check exits 0 when dashboard was just created."""
        data_dir = tmp_path / "data" / "output"
        data_dir.mkdir(parents=True)
        dashboard = data_dir / "dashboard.html"
        dashboard.write_text("<html>ok</html>")
        # File was just written — age is ~0 seconds, well within 1 hour

        result = subprocess.run(
            ["bash", str(HEALTH_CHECK), str(tmp_path / "data")],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}\n{result.stderr}"
        assert "[OK]" in result.stdout

    def test_fails_when_dashboard_missing(self, tmp_path):
        """Health check exits 1 and logs when dashboard.html does not exist."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        result = subprocess.run(
            ["bash", str(HEALTH_CHECK), str(data_dir)],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        assert result.returncode == 1
        assert "[FAIL]" in result.stderr

        failure_log = tmp_path / ".local" / "share" / "morning-brief" / "failures.log"
        assert failure_log.exists(), "failures.log must be created on failure"
        log_content = failure_log.read_text()
        assert "dashboard not found" in log_content

    def test_fails_when_dashboard_stale(self, tmp_path):
        """Health check exits 1 when dashboard.html is older than 1 hour."""
        data_dir = tmp_path / "data" / "output"
        data_dir.mkdir(parents=True)
        dashboard = data_dir / "dashboard.html"
        dashboard.write_text("<html>old</html>")

        # Back-date the file by 2 hours
        two_hours_ago = time.time() - 7200
        os.utime(dashboard, (two_hours_ago, two_hours_ago))

        result = subprocess.run(
            ["bash", str(HEALTH_CHECK), str(tmp_path / "data")],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(tmp_path)},
        )
        assert result.returncode == 1
        assert "[FAIL]" in result.stderr

        failure_log = tmp_path / ".local" / "share" / "morning-brief" / "failures.log"
        assert failure_log.exists()
        log_content = failure_log.read_text()
        assert "stale" in log_content

    def test_failure_log_includes_timestamp_and_exit_code(self, tmp_path):
        """Failure log entries include an ISO timestamp and exit code."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        subprocess.run(
            ["bash", str(HEALTH_CHECK), str(data_dir)],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(tmp_path)},
        )

        failure_log = tmp_path / ".local" / "share" / "morning-brief" / "failures.log"
        log_content = failure_log.read_text()
        # Entry format: <ISO timestamp>  exit=<code>  <message>
        assert "exit=" in log_content
        # Timestamp looks like 2026-03-29T...Z
        assert "T" in log_content and "Z" in log_content

    def test_failure_log_appends(self, tmp_path):
        """Multiple failures are appended, not overwritten."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        env = {**os.environ, "HOME": str(tmp_path)}

        subprocess.run(
            ["bash", str(HEALTH_CHECK), str(data_dir)],
            capture_output=True,
            text=True,
            env=env,
        )
        subprocess.run(
            ["bash", str(HEALTH_CHECK), str(data_dir)],
            capture_output=True,
            text=True,
            env=env,
        )

        failure_log = tmp_path / ".local" / "share" / "morning-brief" / "failures.log"
        lines = [ln for ln in failure_log.read_text().splitlines() if ln.strip()]
        assert len(lines) >= 2, "Each failure should add a new line"


# ── Cross-file consistency checks ────────────────────────────────────────────


class TestConsistency:
    def test_service_references_timer_compatible_name(self):
        """Timer and service share the same base name so systemd links them."""
        assert SERVICE_FILE.stem == TIMER_FILE.stem == "morning-brief"

    def test_health_check_script_referenced_in_service(self):
        service_content = SERVICE_FILE.read_text()
        assert "health-check.sh" in service_content

    @pytest.mark.parametrize("unit_file", [SERVICE_FILE, TIMER_FILE])
    def test_unit_files_have_required_sections(self, unit_file: Path):
        content = unit_file.read_text()
        assert "[Unit]" in content
        assert "[Install]" in content

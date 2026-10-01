"""Tests for failure alerting: the OnFailure= wiring and scripts/notify-failure.sh."""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_FILE = PROJECT_ROOT / "morning-brief.service"
FAILURE_FILE = PROJECT_ROOT / "morning-brief-failure@.service"
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install-user-units.sh"
NOTIFY = PROJECT_ROOT / "scripts" / "notify-failure.sh"


def _sections(text: str) -> dict[str, list[str]]:
    """Split unit-file text into {section: [lines]}."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.fullmatch(r"\[(\w+)\]", line.strip())
        if m:
            current = m.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line.strip())
    return sections


# ── Unit files ────────────────────────────────────────────────────────────────


class TestUnits:
    def test_on_failure_in_unit_section_only(self):
        sections = _sections(SERVICE_FILE.read_text())
        expected = "OnFailure=morning-brief-failure@%n.service"
        assert expected in sections["Unit"]
        assert not any(line.startswith("OnFailure=") for line in sections["Service"])

    def test_output_goes_to_state_file(self):
        service = _sections(SERVICE_FILE.read_text())["Service"]
        assert "StateDirectory=morning-brief" in service
        assert "StandardOutput=truncate:%S/morning-brief/last-run.log" in service
        assert "StandardError=truncate:%S/morning-brief/last-run.log" in service

    def test_failure_unit_exists(self):
        assert FAILURE_FILE.exists(), "morning-brief-failure@.service must exist"

    def test_failure_unit_runs_notify_script_with_instance(self):
        service = _sections(FAILURE_FILE.read_text())["Service"]
        assert "Type=oneshot" in service
        assert (
            "ExecStart=%h/.local/share/morning-brief-pipeline/scripts/notify-failure.sh %i"
            in service
        )

    def test_install_script_links_failure_unit(self):
        content = INSTALL_SCRIPT.read_text()
        assert '"${PIPELINE}/morning-brief-failure@.service"' in content
        link_cmd = content[content.index("systemctl --user link") :].split("\n\n")[0]
        assert "morning-brief-failure@.service" in link_cmd

    def test_notify_script_is_executable(self):
        assert NOTIFY.stat().st_mode & stat.S_IXUSR


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None and not os.environ.get("CI"),
    reason="systemd-analyze not installed (and CI unset)",
)
def test_systemd_analyze_finds_no_unknown_keys(tmp_path):
    assert shutil.which("systemd-analyze"), "systemd-analyze is required under CI"
    units = sorted(PROJECT_ROOT.glob("*.service")) + sorted(PROJECT_ROOT.glob("*.timer"))
    assert units
    copied = []
    for unit in units:
        name = unit.name.replace("@.", "@test.")
        shutil.copy(unit, tmp_path / name)
        copied.append(str(tmp_path / name))
    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", *copied],
        capture_output=True,
        text=True,
    )
    bad = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if "Unknown key" in line or "Unknown section" in line
    ]
    assert not bad, "\n".join(bad)


# ── notify-failure.sh ─────────────────────────────────────────────────────────


def _stub(tmp_path: Path, exit_code: int = 0) -> tuple[Path, Path]:
    """A publisher that records its argv, one argument per record, NUL-separated."""
    record = tmp_path / "record"
    stub = tmp_path / "hub-alert"
    stub.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\0\' "$@" > "' + str(record) + f'"\nexit {exit_code}\n'
    )
    stub.chmod(0o755)
    return stub, record


def _run(stub: Path, log: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HUB_ALERT": str(stub), "MB_RUN_LOG": str(log)}
    return subprocess.run(["bash", str(NOTIFY), *args], env=env, capture_output=True, text=True)


def _log(tmp_path: Path) -> Path:
    log = tmp_path / "last-run.log"
    log.write_text("".join(f"line {i}\n" for i in range(1, 31)))
    return log


class TestNotifyFailure:
    def test_sends_title_priority_and_log_tail(self, tmp_path):
        stub, record = _stub(tmp_path)
        log = _log(tmp_path)
        result = _run(stub, log, "morning-brief.service")
        assert result.returncode == 0, result.stderr
        assert record.exists(), "stub publisher was never executed"
        title, body, priority = record.read_text().split("\0")[:3]
        assert title == "morning-brief failed: morning-brief.service"
        assert priority == "high"
        lines = body.splitlines()
        assert lines[:-1] == [f"line {i}" for i in range(16, 31)]
        assert "line 1" not in lines
        assert lines[-1] == f"Full log: cat {log}"

    def test_body_stays_under_ntfy_limit(self, tmp_path):
        stub, record = _stub(tmp_path)
        log = tmp_path / "last-run.log"
        log.write_text("".join("x" * 500 + "\n" for _ in range(30)))
        assert _run(stub, log, "morning-brief.service").returncode == 0
        body = record.read_text().split("\0")[1]
        assert len(body.encode()) < 3500
        assert body.splitlines()[-1] == f"Full log: cat {log}"

    def test_missing_log_is_reported_in_body(self, tmp_path):
        stub, record = _stub(tmp_path)
        log = tmp_path / "nope.log"
        assert _run(stub, log, "morning-brief.service").returncode == 0
        assert record.exists()
        body = record.read_text().split("\0")[1]
        assert f"(no run log at {log})" in body
        assert body.splitlines()[-1] == f"Full log: cat {log}"

    def test_missing_publisher_fails(self, tmp_path):
        result = _run(tmp_path / "absent", _log(tmp_path), "morning-brief.service")
        assert result.returncode != 0
        assert result.stderr.strip()

    def test_non_executable_publisher_fails(self, tmp_path):
        stub, record = _stub(tmp_path)
        stub.chmod(0o644)
        result = _run(stub, _log(tmp_path), "morning-brief.service")
        assert result.returncode != 0
        assert not record.exists()

    def test_publisher_failure_propagates(self, tmp_path):
        stub, record = _stub(tmp_path, exit_code=1)
        result = _run(stub, _log(tmp_path), "morning-brief.service")
        assert record.exists(), "stub publisher was never executed"
        assert result.returncode != 0
        assert result.stderr.strip()

    def test_unit_argument_required(self, tmp_path):
        stub, record = _stub(tmp_path)
        assert _run(stub, _log(tmp_path)).returncode != 0
        assert not record.exists()

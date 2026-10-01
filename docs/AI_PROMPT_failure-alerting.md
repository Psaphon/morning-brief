# AI Development Prompt — failure-alerting

**Branch:** `fix/failure-alerting` (already checked out)
**Base:** `develop`

Read `CLAUDE.md` for project context and the `## Feature: failure-alerting` section of `docs/DEVPLAN.md` for the full acceptance criteria. Those criteria are the contract.
Do NOT push — the host workflow handles push and PR.

## Why

A failed scheduled run reaches nobody today. On hub the unit runs in hubop's **user** systemd manager, hubop cannot read its own journal, and the user manager's PATH has no `/usr/local/sbin`. The design below works around those three facts. Don't "simplify" them away.

## What to build

1. **`morning-brief.service`** (repo root — NOT `deploy/`; leave `deploy/` untouched):
   - `[Unit]`: add `OnFailure=morning-brief-failure@%n.service`
   - `[Service]`: add `StateDirectory=morning-brief`; replace `StandardOutput=journal` / `StandardError=journal` with
     `StandardOutput=truncate:%S/morning-brief/last-run.log` and `StandardError=truncate:%S/morning-brief/last-run.log`.
     Add a short comment saying why: hubop can't read the journal on hub, and this file always holds the last run.
   - Keep every existing directive and comment otherwise. The existing tests in `tests/test_scheduling.py` must still pass.

2. **`morning-brief-failure@.service`** (new, repo root): `Type=oneshot`,
   `ExecStart=%h/.local/share/morning-brief-pipeline/scripts/notify-failure.sh %i`. Comment style matches `morning-brief.service`.

3. **`scripts/notify-failure.sh`** (new, executable, `set -euo pipefail`, header comment in the style of `scripts/install-user-units.sh`):
   - Arg 1: the failed unit name (e.g. `morning-brief.service`); required.
   - Publisher: `HUB_ALERT="${HUB_ALERT:-/usr/local/sbin/hub-alert}"`. If it's missing or not executable → message to stderr, exit 1.
   - Log: `LOG="${MB_RUN_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/morning-brief/last-run.log}"`.
   - Title: `morning-brief failed: <unit>`.
   - Body: the last 15 lines of the log (or `(no run log at <path>)` when it's absent), then a final line `Full log: cat <path>`. Keep the body under 3500 bytes (ntfy limit); trim from the top.
   - Call `"$HUB_ALERT" "$title" "$body" high`. If it exits non-zero → stderr message, exit non-zero. Never hardcode an ntfy URL or host.

4. **`scripts/install-user-units.sh`**: also `systemctl --user link` `morning-brief-failure@.service` from the pipeline clone.

5. **Tests** (new `tests/test_failure_alerting.py`; follow the patterns in `tests/test_scheduling.py`):
   - Unit text: `OnFailure=` appears in `[Unit]` and not in `[Service]` (parse sections; don't just substring-match).
   - The failure unit exists, its `ExecStart` points into the pipeline clone's `scripts/notify-failure.sh` and passes `%i`.
   - `install-user-units.sh` links the failure unit.
   - `systemd-analyze --user verify`: copy every repo-root `*.service`/`*.timer` into a temp dir (template units under an instance name, e.g. `morning-brief-failure@test.service`), run verify, and fail on any line containing `Unknown key` or `Unknown section`. Ignore other output (missing executables are expected). Skip ONLY when `shutil.which("systemd-analyze")` is None AND `os.environ.get("CI")` is unset; under CI, a missing tool is a failure.
   - `notify-failure.sh`: a stub publisher that appends its argv to a record file. Run the script with `HUB_ALERT=<stub>` and `MB_RUN_LOG=<fixture log of 30 lines>`; assert title, priority `high`, the body holds the last 15 lines and not line 1, and ends with the `Full log:` line. **Assert the record file exists**: if the stub couldn't execute, the test must fail, not pass. Also: missing publisher → non-zero exit; stub that exits 1 → non-zero exit; missing log → body says `(no run log at …)`.
   - Create stubs under `tmp_path` and `chmod 0o755` them. Hub mounts `/tmp` noexec; hub's CI wrapper already points pytest's basetemp at an exec-able dir, so don't work around that yourself.

6. **Mutation check** (do it, don't commit it): move `OnFailure=` into `[Service]` → suite red; delete `morning-brief-failure@.service` → red; drop the link line from `install-user-units.sh` → red. Restore after each one. Report the three results in your final message.

7. **DEVPLAN:** tick the acceptance criteria you met (not the `[HUMAN]` one) in the `failure-alerting` section. Leave `Status:` alone.

## Commit

One commit:

```
fix: alert the phone when a scheduled morning-brief run fails
```

## Rules

- Run `ruff check . && ruff format --check .` and `pytest --tb=short` before the commit; both must pass
- Do NOT push
- Do NOT modify files outside the list above (`deploy/` included)
- Do NOT read `.env` or anything under `secrets/`, and do not read `/etc/hub/ntfy.env`

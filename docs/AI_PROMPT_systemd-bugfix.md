# AI Development Prompt — systemd-bugfix

**Branch:** `fix/systemd-units`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` for full acceptance criteria.
Do NOT push — the host workflow handles push and PR.

## What to build

Fix bugs in the systemd unit files under `deploy/` that prevent them from working as user-level units. The root-level `morning-brief.service` and `morning-brief.timer` already have correct versions — use them as reference.

### deploy/morning-brief.timer

1. Fix `OnCalendar` timezone syntax — the timezone goes in a separate `TimeZone=` directive, not inline. Change:
   ```
   OnCalendar=America/New_York *-*-* 04:15:00
   ```
   to:
   ```
   OnCalendar=*-*-* 04:15:00
   TimeZone=America/New_York
   ```
2. Remove `Requires=morning-brief.service` from `[Unit]` — timers implicitly activate their matching service

### deploy/morning-brief.service

1. Remove `Requires=docker.service` — this is a system-level unit and can't be referenced from a user-level service
2. Change `After=docker.service network-online.target` to `After=network-online.target`
3. Change `WorkingDirectory=/opt/morning-brief` to `WorkingDirectory=%h/Projects/morning-brief`
4. Add `Environment=DEPLOY_ENABLED=true` in the `[Service]` section
5. Change `WantedBy=multi-user.target` to `WantedBy=default.target` (user-level target)

### Verification

After making changes, run:
```bash
systemd-analyze verify deploy/morning-brief.service deploy/morning-brief.timer 2>&1 || true
```
Note: this may warn about missing units on a dev machine — that's expected. The key is no syntax errors.

## Commit message

```
fix: correct systemd unit files for user-level operation

Fix deploy/ unit files: OnCalendar timezone syntax, remove
docker.service dependency, use %h for WorkingDirectory, add
DEPLOY_ENABLED env var, target default.target for user units.
```

## Rules

- Run `ruff check . && ruff format --check .` before committing
- Run `pytest tests/ -v` before committing
- Do NOT push
- Do NOT modify files outside `deploy/morning-brief.service` and `deploy/morning-brief.timer`
- Do NOT modify the root-level `morning-brief.service` or `morning-brief.timer` — those are already correct

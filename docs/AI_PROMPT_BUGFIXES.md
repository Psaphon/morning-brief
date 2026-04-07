# AI Development Prompt — Bugfixes

**Branch:** `fix/systemd-units`
**Base:** `develop`

Read `CLAUDE.md` for project context. Make one commit for this fix. Do NOT push — the host workflow handles push and PR.

---

## Bugfix: systemd unit files

The timer and service files in `deploy/` have bugs that prevent them from working as user-level systemd units.

### What to fix

1. **`deploy/morning-brief.timer`** — `OnCalendar` timezone syntax is wrong.
   - Current: `OnCalendar=America/New_York *-*-* 04:15:00`
   - Correct: `OnCalendar=*-*-* 04:15:00 America/New_York`
   - Timezone goes at the END, not the beginning.

2. **`deploy/morning-brief.service`** — References `docker.service` which is a system-level unit. User-level systemd cannot depend on system-level units.
   - Remove `Requires=docker.service`
   - Change `After=docker.service network-online.target` to `After=network-online.target`
   - Change `WorkingDirectory=/opt/morning-brief` to `WorkingDirectory=%h/Projects/morning-brief`

### Verification

Run `systemd-analyze verify deploy/morning-brief.service deploy/morning-brief.timer` to confirm no errors.

**Commit as:** `fix: correct systemd timer syntax and service dependencies`

---

## Rules

- Run `ruff check . && ruff format --check .` before committing. Fix all issues.
- Run `python -m pytest tests/ -v` before committing. All tests must pass.
- Do NOT push — the host workflow handles push and PR creation.
- Do NOT modify files outside the scope of this fix.

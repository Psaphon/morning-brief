# Coordination Tasks

External dependencies on morning-brief from other projects. Not features — coordination the PM must track.

---

## Pending

### Timer move: 04:15 → 05:30 (for loom GPU handoff)

**Flagged:** 2026-04-12
**Source:** loom project, `systemd-integration` feature (`Requires: both`)
**Blocks:** loom's nightly render window (00:00–05:30)

**Change.** `morning-brief.timer` `OnCalendar=*-*-* 04:15:00` → `*-*-* 05:30:00`.

**Why.** Loom owns the GPU 00:00–05:30 and restarts Ollama on exit. Morning-brief can't start until Ollama is warm.

**Sequencing.** Must land **at the same time** loom's systemd units are installed, not before (would lose the 04:15 slot for no reason) and not after (would collide with loom). Treat as a paired deploy.

**Action.** One-line edit in `morning-brief.timer`, normal PR to develop.

#!/usr/bin/env bash
# scripts/run-pipeline.sh — build and run the pipeline container once
#
# Called by morning-brief.service (ExecStart). Picks the container user so the
# pipeline can write the bind-mounted data/ directory:
#   - rootless Docker: 0:0. Container uid 0 IS the host user there; any other
#     uid maps to an unrelated subordinate id that cannot write data/.
#   - rootful Docker: the host user's own uid:gid.
# Either way the container keeps cap_drop ALL (see docker-compose.yml).
#
# Exit code is the pipeline container's exit code.

set -euo pipefail

cd "$(dirname "$0")/.."

if docker info --format '{{json .SecurityOptions}}' 2>/dev/null | grep -q 'name=rootless'; then
    uid=0 gid=0
else
    uid=$(id -u) gid=$(id -g)
fi

export MB_UID="${uid}" MB_GID="${gid}"
docker compose up --build --abort-on-container-exit --exit-code-from morning-brief

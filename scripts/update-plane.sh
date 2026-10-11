#!/usr/bin/env bash
# Move the pinned control-plane runtime to origin/main.
#
# Every `genesis serve` on this machine runs from this clone, never from a
# checkout an agent works in. Genesis's evolver edits genesis, and a plane
# importing from the evolver's own checkout would run whatever branch the
# evolver last switched to the next time it restarted. Updating is a deliberate
# act, and this script is that act. Restart running planes afterwards.
set -euo pipefail

PLANE="${GENESIS_PLANE_DIR:-$HOME/git/genesis-plane}"

if [ ! -d "$PLANE/.git" ]; then
  git clone -q https://github.com/Sayfan-AI/genesis.git "$PLANE"
fi
git -C "$PLANE" fetch -q origin
git -C "$PLANE" checkout -q --detach origin/main
(cd "$PLANE" && uv sync --locked -q)
echo "plane runtime at $(git -C "$PLANE" log --oneline -1)"
echo "run planes as: $PLANE/.venv/bin/genesis serve"

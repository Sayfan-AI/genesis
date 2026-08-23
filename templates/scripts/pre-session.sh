#!/usr/bin/env bash
# Deterministic checks that must run BEFORE the agent gets its first turn.
#
# Seeded empty on purpose. What belongs here is specific to what this dev system
# has learned about itself, and genesis can't guess it. What genesis can do is
# make sure there's a seam that fires in every execution mode, because that turned
# out to be the hard part.
#
# WHY THIS FILE EXISTS. A dev system is taught to turn a check that needs no
# judgement into a script and wire it ahead of the agent step in workflow YAML.
# That's right, and it silently stops being true under `genesis serve`, which
# disables every `genesis-*` workflow and launches the session directly. Measured
# (#44): a `nudge-gates.sh` written precisely because an unanswered `needs:human`
# gate is the one failure with no safety net — no failing run, no red check, no
# signal at all — and then a gate sat open 21 days across about 85 scheduled
# ticks, because the step that would have caught it only existed in a workflow
# that local mode had switched off.
#
# So this file is wired two ways and runs exactly once either way: declared on
# `SessionStart` in .claude/settings.json, which the harness fires in Actions and
# under serve alike, and invoked directly by `genesis serve` before it launches a
# session, for the case where the hook isn't declared. Serve checks the settings
# file and skips its own call when the hook is there.
#
# CONTRACT, and none of it is optional:
#   - Idempotent. It runs before every session, including resumes.
#   - Fast. It's on the critical path of every session start.
#   - Non-fatal. A non-zero exit is logged and the session starts anyway; a net
#     that can stop the loop it protects is worse than the gap it fills.
#   - Deterministic. If a check needs judgement it belongs in an agent, not here.
#
# Compose several checks inside this one script rather than asking for more
# conventional paths — a list of files defaults its next member to unwired, which
# is the shape that produced this issue in the first place.
set -uo pipefail

# Nothing yet. Add checks below, e.g.:
#
#   bash .genesis/scripts/nudge-gates.sh || true
#   bash .genesis/scripts/issues.sh unselectable-work

exit 0

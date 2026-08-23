#!/usr/bin/env bash
# Genesis .claude/ gate — turn an unactionable permission stall into a named
# request. Wired to PreToolUse, which can block a call by exiting 2.
#
# WHY THIS EXISTS, and why it is a message rather than a rule: the harness
# already refuses these writes. This guard adds no restriction. What it adds is
# an *instruction*, delivered at the one moment the agent is about to give up.
#
# Without it the agent gets "Claude requested permissions to write to <path>, but
# you haven't granted it yet" and stops. Under `genesis serve` there is nobody to
# ask, so the task dies holding a change it can describe perfectly well. Measured
# downstream (MaKlaude issue #208): four separate fixes blocked this way. Three
# had a fallback, because CLAUDE.md carries prose in any mode. The fourth needed
# a hook declared in .claude/settings.json, which is not prose and has no second
# home, so a milestone task sat waiting on a human to paste two lines of JSON —
# a human *edit* rather than a human *decision*, which is the shape this
# framework exists to remove.
#
# WHAT WAS MEASURED, so nobody re-litigates the mechanism from documentation.
# Every row below is a real revertible write attempt against a scratch repo:
#
#   allowedTools Read,Write,Edit ................................. denied
#   + permissions.allow in the repo .claude/settings.json ........ denied
#     (and reported "Ignoring N permissions.allow entries from
#      .claude/settings.json: this workspace has not been trusted")
#   + the same allow-list passed by the operator via --settings ... denied
#   + the workspace trusted, so the allow entries load ............ denied
#   --permission-mode acceptEdits ................................ denied
#   --permission-mode dontAsk .................................... denied
#   Bash redirect: printf ... >> .claude/agents/worker.md ........ denied
#   --permission-mode bypassPermissions .......................... ALLOWED
#   control: same tools, a file outside .claude/ ................. allowed
#
# So the protection is path-based and applies across tools, and no allow-list
# relaxes it. The one lever that works is blanket bypass, which hands a session
# the ability to rewrite its own operating rules — a worse problem than the one
# being solved. Hence a gate, not a grant.
#
# PreToolUse fires BEFORE the permission decision (measured: the hook ran on a
# call the harness went on to deny), which is what makes this possible at all.
#
# Fails OPEN, like host-guard.sh: a bug here must not wedge the loop.
set -uo pipefail

RAW="$(cat)"

python3 - "$RAW" <<'PY'
import json, re, sys

raw = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    ctx = json.loads(raw) if raw.strip() else {}
except (ValueError, TypeError):
    sys.exit(0)  # unparseable payload: fail open, never block on our own bug
if not isinstance(ctx, dict):
    sys.exit(0)  # valid JSON of the wrong shape is still not something to act on

tool = ctx.get("tool_name") or ""
tool_input = ctx.get("tool_input") or {}
if not isinstance(tool_input, dict):
    sys.exit(0)


def under_claude_dir(path):
    """True when `path` lands inside the repo's own .claude/ directory.

    Matched on the path as written rather than resolved, because resolving means
    touching the filesystem from a hook that runs on every single tool call. The
    shapes that matter are the ones an agent actually emits: a repo-relative
    path, a ./-prefixed one, and the absolute path the harness echoes back.
    """
    if not isinstance(path, str) or not path:
        return False
    normalised = path.replace("\\", "/")
    return bool(re.search(r"(^|/)\.claude(/|$)", normalised))


# File-writing tools name their target directly. Read is deliberately absent: an
# agent reading its own agent definition is not only allowed, it is how the
# evolver knows what to change.
if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    hit = under_claude_dir(target)
elif tool == "Bash":
    # Only redirection counts. `grep -rn foo .claude/` and `cat
    # .claude/settings.json` are reads and must stay allowed - a guard that
    # refuses them teaches the agent to route around guards, which is the exact
    # failure mode host-guard.sh's notes warn about.
    command = tool_input.get("command") or ""
    if not isinstance(command, str):
        sys.exit(0)
    hit = bool(re.search(r">>?\s*['\"]?[^\s'\"|;&]*(^|/)?\.claude/", command)) or bool(
        re.search(r"\b(tee|install|cp|mv|touch|mkdir)\b[^|;&]*?(^|[\s'\"=])\.?/?\.claude/", command)
    )
    target = command
else:
    sys.exit(0)

if not hit:
    sys.exit(0)

sys.stderr.write(
    "blocked by .genesis/scripts/claude-dir-guard.sh: the harness does not allow "
    f"writes under .claude/ ({target!r}), and no permissions entry, settings file "
    "or permission mode relaxes it. Retrying in another shape will not work.\n"
    "\n"
    "Do this instead, and treat it as completing the step rather than failing it:\n"
    "  1. Comment on the task issue with the exact edit you intended, as a fenced\n"
    "     diff or the full file content - not a description of it. Someone has to\n"
    "     be able to apply it without reconstructing your reasoning.\n"
    "  2. Say which file it belongs in and why it cannot live anywhere else.\n"
    "  3. Label the issue `needs:human`.\n"
    "  4. Carry on with the rest of the task.\n"
    "\n"
    "If the change is prose - a rule, a convention, an instruction to a future\n"
    "agent - CLAUDE.md is a real alternative home and reaches agents in both\n"
    "execution modes. Hook wiring and agent front-matter have no alternative home,\n"
    "which is why they need the comment.\n"
)
sys.exit(2)  # PreToolUse: 2 blocks the call
PY

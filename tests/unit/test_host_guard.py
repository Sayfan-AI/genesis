"""Behavior tests for templates/scripts/host-guard.sh.

The guard is wired to `PreToolUse`, where exit 2 blocks the call. It exists for the
accidental case that actually happened: an agent hunting for a shell alias ran a
recursive grep whose glob covered a file holding work credentials.

Two failure directions matter here and they do NOT cost the same, which is why the
matrix below is written as pairs rather than as a list of things that should block:

- A **false negative** is a hole. The guard is the only thing between a careless
  command and the operator's secrets.
- A **false positive** is a nuisance that becomes a hole indirectly. A guard that
  refuses legitimate work teaches the agent to route around guards, which is the
  behavior this project's own notes warn about for the deterministic nets.

The false positive was real and reported (MaKlaude issue #211): the guard matched
its credential-path patterns against the whole command string, so a heredoc writing
*documentation about the guard* was refused as though it had read a credential file.
The obvious fix, exempting heredoc bodies, opens the bypass pinned by
`test_a_heredoc_piped_into_an_interpreter_is_still_code` below. That test is the
reason this file exists rather than a one-line patch.
"""

import json
import subprocess
from pathlib import Path

import pytest


GUARD = Path(__file__).parents[2] / "templates" / "scripts" / "host-guard.sh"

BLOCKED = 2  # PreToolUse treats exit 2 as "refuse this call"
ALLOWED = 0


def run_guard(command, tool_name="Bash"):
    """Feed the guard a PreToolUse payload and return (exit code, stderr)."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    proc = subprocess.run(
        ["bash", str(GUARD)], input=payload, capture_output=True, text=True
    )
    return proc.returncode, proc.stderr


class TestReadsAreRefused:
    """The cases the guard exists for."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat ~/.ssh/id_rsa",
            'grep -rn "gci" ~/.zshrc ~/.dotfiles.local/*.sh',  # the real incident
            "grep -r token $HOME/.aws/credentials",
            "cp ~/.gnupg/secring.gpg /tmp/",
            "cat ~/.netrc",
            "cat ~/.config/gh/hosts.yml",
            "cat ~/.claude.json",
            "ls ~/Library/Keychains",
            "cat /etc/shadow",
        ],
    )
    def test_credential_paths_are_blocked(self, command):
        code, err = run_guard(command)
        assert code == BLOCKED, f"not blocked: {command!r}"
        assert "host-guard.sh" in err

    def test_the_expanded_home_spelling_is_blocked_too(self):
        """~ and $HOME and /Users/me are the same directory."""
        import os

        code, _ = run_guard(f"cat {os.path.expanduser('~')}/.ssh/id_rsa")
        assert code == BLOCKED


class TestOrdinaryWorkIsAllowed:
    """A guard that refuses real work gets routed around."""

    @pytest.mark.parametrize(
        "command",
        [
            "go test ./...",
            "ls .genesis/scripts",
            "gh issue list --label milestone:6",
            "git log --oneline -5",
            "cat ~/.kube/config",  # deliberately NOT guarded: reading this is the job
            "kubectl get pods -n default",
        ],
    )
    def test_benign_commands_pass(self, command):
        code, err = run_guard(command)
        assert code == ALLOWED, f"false positive on {command!r}: {err}"

    def test_non_bash_tools_are_not_inspected(self):
        """The guard only understands shell commands."""
        code, _ = run_guard("cat ~/.ssh/id_rsa", tool_name="Read")
        assert code == ALLOWED


class TestProseIsNotARead:
    """MaKlaude issue #211. Naming a path is not reading it."""

    def test_a_heredoc_writing_documentation_is_allowed(self):
        """The reported false positive, and the guard's own docs are the victim."""
        command = (
            "cat > /tmp/pr.md <<'EOF'\n"
            "The guard refuses commands that reach for ~/.ssh, ~/.aws or ~/.netrc.\n"
            "EOF"
        )
        code, err = run_guard(command)
        assert code == ALLOWED, f"prose still refused: {err}"

    @pytest.mark.parametrize("sink", ["cat > /tmp/x.md", "tee /tmp/x.md", "printf '%s'"])
    def test_other_text_sinks_are_allowed(self, sink):
        code, _ = run_guard(f"{sink} <<'EOF'\nwe block ~/.ssh here\nEOF")
        assert code == ALLOWED

    def test_gh_reading_a_body_from_stdin_is_allowed(self):
        """How the loop actually posts an issue comment about the guard."""
        command = (
            "gh issue comment 211 --body-file - <<'EOF'\n"
            "This blocks reads of ~/.aws/credentials.\n"
            "EOF"
        )
        code, _ = run_guard(command)
        assert code == ALLOWED

    def test_an_indented_heredoc_terminator_is_understood(self):
        """<<- strips leading tabs, so the terminator is matched after stripping."""
        command = "cat <<-'EOF'\n\tmentions ~/.ssh in prose\n\tEOF"
        code, _ = run_guard(command)
        assert code == ALLOWED


class TestTheExemptionCannotBeUsedAsABypass:
    """The reason the discriminator is what consumes the body, not whether it is a
    heredoc. Each of these must stay blocked."""

    def test_a_heredoc_fed_to_an_interpreter_is_still_code(self):
        """Exempting heredoc bodies wholesale makes this a one-line evasion."""
        command = "bash <<'EOF'\ncat ~/.ssh/id_rsa\nEOF"
        code, err = run_guard(command)
        assert code == BLOCKED, "bypass: interpreter heredoc was exempted"
        assert "host-guard.sh" in err

    @pytest.mark.parametrize("interp", ["sh", "zsh", "python3", "node", "perl", "ruby"])
    def test_no_interpreter_gets_the_exemption(self, interp):
        code, _ = run_guard(f"{interp} <<'EOF'\ncat ~/.ssh/id_rsa\nEOF")
        assert code == BLOCKED, f"bypass via {interp}"

    def test_a_heredoc_piped_into_an_interpreter_is_still_code(self):
        """The sink name is `cat`, which is allowlisted. The pipe is what matters."""
        command = "cat <<'EOF' | bash\ncat ~/.ssh/id_rsa\nEOF"
        code, _ = run_guard(command)
        assert code == BLOCKED, "bypass: allowlisted sink piped to an interpreter"

    def test_writing_a_script_then_running_it_is_still_code(self):
        """A chain writes the body to a file and then executes that file."""
        command = "cat > /tmp/x.sh <<'EOF' ; bash /tmp/x.sh\ncat ~/.ssh/id_rsa\nEOF"
        code, _ = run_guard(command)
        assert code == BLOCKED, "bypass: write-then-execute chain"

    def test_command_substitution_keeps_the_body_in_scope(self):
        command = "cat > $(echo /tmp/x.sh) <<'EOF'\ncat ~/.ssh/id_rsa\nEOF"
        code, _ = run_guard(command)
        assert code == BLOCKED, "bypass: substitution in the opener"

    def test_an_unterminated_heredoc_strips_nothing(self):
        """Not a well-formed heredoc, so there is no body to classify.

        Three lines, not two, and that is the whole point of the case. With a
        two-line command the credential read is the LAST line, so an
        implementation that guessed the terminator as "end of input" would keep
        that line as the terminator and still block, and the test would pass over
        a real hole. Mutation testing caught exactly that: substituting
        `end = len(lines) - 1` for the `continue` left this suite green. With a
        trailing line the mis-guessed body swallows the read.
        """
        command = "cat > /tmp/x.md <<'EOF'\ncat ~/.ssh/id_rsa\ntrailing line"
        code, _ = run_guard(command)
        assert code == BLOCKED

    def test_a_read_outside_the_heredoc_is_still_seen(self):
        """Stripping the body must not blind the guard to the rest of the line."""
        command = "cat ~/.ssh/id_rsa; cat > /tmp/x.md <<'EOF'\nharmless prose\nEOF"
        code, _ = run_guard(command)
        assert code == BLOCKED


class TestFailsOpenOnItsOwnBugs:
    """A broken guard must not wedge every session. That is a deliberate trade, and
    it means a crash degrades to no protection silently, so these assert exit 0
    rather than merely 'did not crash'."""

    @pytest.mark.parametrize(
        "payload",
        ['{"tool_name": "Bash"}', "{}", "not json at all", "", '{"tool_input": null}'],
    )
    def test_malformed_payloads_are_allowed_not_crashed(self, payload):
        proc = subprocess.run(
            ["bash", str(GUARD)], input=payload, capture_output=True, text=True
        )
        assert proc.returncode == ALLOWED, f"payload {payload!r} did not fail open"

    def test_a_non_string_command_is_allowed(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": 42}})
        proc = subprocess.run(
            ["bash", str(GUARD)], input=payload, capture_output=True, text=True
        )
        assert proc.returncode == ALLOWED

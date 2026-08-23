"""What the seeded agent definitions commit a fresh dev system to.

Every assertion here is on the *mechanism* a rule names, not on the sentence it
happens to be written in. The cautionary example is in `tests/e2e/test_workflows.py`:
a bot-filter test that checked for the literal `github-actions[bot]` passed for the
entire life of a bug, because the broken enumerated-list version contained that
string too. So these tests look for the thing that makes each rule work — the
`--all` that makes a milestone check see the closed batch, the label that
`issues.sh next` actually filters on, the `mergedAt` that separates merged from
closed — and cross-check the two files that have to agree about it.

Covers genesis issues #8, #10, #30 and #33 (the onboarding half, MaKlaude issue #3).
"""

import re
from pathlib import Path

import pytest


TEMPLATES = Path(__file__).parents[2] / "templates"

NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _section(text: str, heading: str) -> str:
    """The body of a `## heading` section, up to the next `## ` heading."""
    match = re.search(
        rf"^##[ ]{re.escape(heading)}[ ]*$(.*?)(?=^##[ ]|\Z)",
        text,
        re.M | re.S,
    )
    assert match, f"no '## {heading}' section found"
    return match.group(1)


def _bullet(text: str, needle: str) -> str:
    """The single list item that talks about `needle`.

    Every list item in these templates is one long line, so a line is the unit.
    """
    hits = [ln for ln in text.splitlines() if needle.lower() in ln.lower()]
    assert hits, f"nothing mentions {needle!r}"
    assert len(hits) == 1, f"{needle!r} is discussed in {len(hits)} places: {hits}"
    return hits[0]


def _labels_the_selector_skips() -> set[str]:
    """The labels `issues.sh next` refuses to hand out, read out of the script."""
    script = (TEMPLATES / "scripts" / "issues.sh").read_text()
    block = re.search(r"^    next\)(.*?)^        ;;", script, re.M | re.S)
    assert block, "issues.sh has no `next` subcommand any more"
    return set(re.findall(r'index\("([^"]+)"\)', block.group(1)))


@pytest.fixture(scope="module")
def orchestrator() -> str:
    return (TEMPLATES / "agents" / "orchestrator.md").read_text()


class TestAMilestoneIsBrokenDownOnce:
    """Issue #8 — duplicate task issues, filed by a *later* run.

    The concurrency group shared by `genesis-orchestrator.yml` and
    `genesis-events.yml` (issues #11 and #22) rules out two runs racing to create
    the same set. It does nothing about the shape actually measured on
    ronny-learns-ai: one run creates the breakdown, a later run creates it again,
    because "the human closed the plan issue" stays true forever and reads as a
    standing instruction to create task issues.
    """

    def test_the_check_reads_closed_issues_too(self, orchestrator: str) -> None:
        """`--all`, not the default open-only listing.

        By the time the duplicate batch is filed, the first batch may be closed.
        A check that only sees open issues sees an empty milestone and rebuilds
        it — which is the bug, not a near miss.
        """
        rules = _section(orchestrator, "Hard Rules (MUST follow)")
        assert re.search(r"list --milestone \S+ --all", rules), (
            "the Hard Rules must tell the run to list the whole milestone "
            "including closed issues before creating a task breakdown"
        )

    def test_the_step_that_creates_the_issues_carries_the_guard(
        self, orchestrator: str
    ) -> None:
        """The re-firing sentence is in Responsibilities, so the guard goes there.

        "Once the plan issue is closed, create the concrete task issues" is what
        a later run re-executes. A Hard Rule alone leaves that line still saying
        the wrong thing, and the two would drift apart on the next rewrite.
        """
        step = _bullet(_section(orchestrator, "Responsibilities"), "Execute approved plan")
        assert "--all" in step, (
            "the responsibility that creates task issues must point at the "
            f"milestone check: {step}"
        )

    def test_the_guard_is_a_lookup_not_a_similarity_judgment(
        self, orchestrator: str
    ) -> None:
        """Issue #8 proposed matching normalized titles. That's the weaker guard.

        The measured recurrence titled the same task two different ways, so a
        title comparison had nothing to catch it with. "Does this milestone have
        a breakdown" is a fact; "is this the same task" is an opinion, and the
        opinion-shaped rule is the one that had already been added and ignored.
        """
        rule = _bullet(_section(orchestrator, "Hard Rules (MUST follow)"), "--all")
        assert "milestone" in rule.lower()
        assert not re.search(r"if a similar issue", rule, re.I), (
            "the duplicate guard must key on milestone state, not on whether a "
            f"title looks similar: {rule}"
        )


class TestDeclaredDependenciesBecomeLabels:
    """Issue #10 — butterfly shipped three parallel PRs against a moving API.

    The half about dispatching several tasks at once, and about a second worker
    landing on a task that already has a PR, is already handled by mechanism:
    `issues.sh next` returns exactly one issue and claims it `in-progress` in the
    same verified call, so the task it handed out cannot be handed out again.
    What no mechanism covers is *ordering* — `next` will happily hand out task B
    while task A's PR is open, because nothing has told it B is waiting.
    """

    def test_the_dependency_rule_uses_a_label_the_selector_actually_skips(
        self, orchestrator: str
    ) -> None:
        """The point of the rule is to feed the existing selector, not add a check.

        A dependency written down as `blocked` is enforced by `issues.sh next` on
        every future run with nobody remembering it. If `next` ever stops
        filtering on that label, this rule silently becomes decoration — hence
        reading the label set out of the script rather than hard-coding it.
        """
        skipped = _labels_the_selector_skips()
        assert "blocked" in skipped, (
            f"issues.sh next no longer skips `blocked` (it skips {sorted(skipped)}); "
            "the orchestrator's dependency rule depends on it"
        )
        rule = _bullet(_section(orchestrator, "Hard Rules (MUST follow)"), "mergedAt")
        used = set(re.findall(r"`([^`]+)`", rule))
        assert used & skipped, (
            "the dependency rule names no label that `issues.sh next` filters on, "
            f"so nothing enforces it: rule mentions {sorted(used)}, "
            f"selector skips {sorted(skipped)}"
        )

    def test_only_a_merge_unblocks_not_a_close(self, orchestrator: str) -> None:
        """`--json mergedAt` is the only field that tells the two apart.

        butterfly PR #40 and PR #41 were written against a trait that PR #38
        introduced. A closed-unmerged prerequisite leaves that trait nonexistent,
        so "the PR is resolved" is not the condition — "the PR is merged" is.
        """
        rules = _section(orchestrator, "Hard Rules (MUST follow)")
        assert "mergedAt" in rules, (
            "nothing in the Hard Rules distinguishes a merged prerequisite from a "
            "closed one; `gh pr view <n> --json mergedAt` is what does"
        )

    def test_dispatch_is_the_one_issue_the_selector_returned(
        self, orchestrator: str
    ) -> None:
        """butterfly's run dispatched five tasks at once against this same file.

        The Hard Rules already say one unit per run; the Responsibilities said
        "execute ready tasks", plural. Two instructions, one of them wrong.
        """
        step = _bullet(_section(orchestrator, "Responsibilities"), "**Dispatch**")
        assert "next" in step, f"dispatch must name the selector it draws from: {step}"


class TestOnboardingDoesNotPreCommitAnArchitecture:
    """Issue #30 — MaKlaude's onboarding defaulted to a fixed multi-agent roster.

    Both carriers a fresh system reads during onboarding have to say it, and the
    prohibition has to bind the *default*: async onboarding runs on "silence =
    accept defaults", so an architecture offered as a default is an architecture
    chosen by nobody.
    """

    ROLE_WORDS = ("architecture", "roster", "org chart", "coordinator")

    @pytest.mark.parametrize(
        "template",
        ["agents/human_interaction.md", "onboarding_issue.md.j2"],
    )
    def test_architecture_only_ever_comes_up_to_be_forbidden(self, template: str) -> None:
        """Both directions in one assertion.

        Delete the prohibition and there are no mentions left, which fails. Add a
        step that *invites* an architecture — "propose the initial agent roster",
        the MaKlaude shape — and there's a mention with no negation, which also
        fails. A test that only looked for the prohibition's own wording would
        pass in the second case, which is the one that ships the bug.
        """
        text = (TEMPLATES / template).read_text()
        mentions = [
            line
            for line in text.splitlines()
            if any(word in line.lower() for word in self.ROLE_WORDS)
        ]
        assert mentions, f"{template} says nothing about keeping architecture out of onboarding"
        for line in mentions:
            assert re.search(r"\bnot\b", line, re.I), (
                f"{template} raises architecture without ruling it out: {line}"
            )

    def test_the_only_default_onboarding_offers_is_the_comms_channel(self) -> None:
        """Async onboarding runs on "silence = accept defaults".

        So a default isn't a suggestion, it's the answer that ships when nobody
        replies — which is how MaKlaude's sixth question put a coordinator and two
        named roles into a project that had no code yet. Exactly one step of the
        flow is allowed to offer the human a default, and it's the one about how
        to reach them.
        """
        section = _section(
            (TEMPLATES / "agents" / "human_interaction.md").read_text(),
            "Onboarding (your first task)",
        )
        steps = [ln for ln in section.splitlines() if re.match(r"^\d+\. ", ln)]
        assert steps, "the onboarding flow has no numbered steps any more"
        with_defaults = [ln for ln in steps if "default" in ln.lower()]
        assert len(with_defaults) == 1, (
            f"{len(with_defaults)} onboarding steps offer the human a default: {with_defaults}"
        )
        assert "communicate" in with_defaults[0].lower(), (
            "the only question onboarding may answer on the human's behalf is how "
            f"to reach them: {with_defaults[0]}"
        )

    def test_the_seeded_claude_md_replaces_the_roster_with_questions(self) -> None:
        """Deleting the roster can't leave a fresh system with a blank page.

        What replaces it is the set of questions a role has to answer before it
        exists — scaffolding that doesn't decide anything in advance.
        """
        section = _section((TEMPLATES / "claude_md.md.j2").read_text(), "Agents")
        questions = [ln for ln in section.splitlines() if ln.startswith("- ") and "?" in ln]
        assert len(questions) >= 3, (
            f"the Agents section offers {len(questions)} questions to justify a new "
            "role; removing the roster without them just leaves a blank page"
        )

    def test_the_evolver_and_claude_md_agree_on_how_many_questions(self) -> None:
        """The evolver is the agent that introduces roles, and it points here.

        A count that drifts sends it looking for questions that aren't there.
        """
        evolver = (TEMPLATES / "agents" / "evolver.md").read_text()
        claimed = re.search(r"the (\w+) questions under \"Agents\"", evolver)
        assert claimed, "evolver.md no longer routes new-role proposals to CLAUDE.md"
        section = _section((TEMPLATES / "claude_md.md.j2").read_text(), "Agents")
        actual = len([ln for ln in section.splitlines() if ln.startswith("- ") and "?" in ln])
        assert NUMBER_WORDS[claimed.group(1)] == actual, (
            f"evolver.md says {claimed.group(1)} questions, CLAUDE.md asks {actual}"
        )


class TestOnboardingCannotClearItsOwnGate:
    """MaKlaude issue #3 — the onboarding agent's definition walked it through
    both of the human gates it exists to stop at.

    Its numbered flow ended `7. Create GitHub issues for milestone 1 tasks` and
    `8. Close issue #1`. Closing that issue is the human's approval of the
    roadmap and nothing else signals it, so step 8 approved the roadmap on the
    human's behalf and step 7 started building against it. The async run on issue
    #1 escaped only because it followed the instructions embedded in the issue
    rather than the ones in its own agent definition — which is the sharpest
    thing about the bug: the two carriers disagreed, and the system happened to
    read the correct one.

    So both are checked, and structurally. A prohibition is prose an agent can
    reason its way around; a numbered flow that ends at the gate is a shape.
    """

    GATE_CARRIERS = ("agents/human_interaction.md", "onboarding_issue.md.j2")

    @pytest.mark.parametrize("carrier", GATE_CARRIERS)
    def test_the_close_that_approves_the_roadmap_is_the_humans(self, carrier: str) -> None:
        """The one sentence that makes the gate a gate, in both carriers.

        Without it, "label `needs:human` and stop" reads as a notification rather
        than a handover, and an agent that later re-reads the file has no reason
        not to close the issue it just labelled.
        """
        text = (TEMPLATES / carrier).read_text()
        assert re.search(r"the human clos\w+", text, re.I), (
            f"{carrier} never says the human's close is what approves the roadmap"
        )

    @pytest.mark.parametrize("carrier", GATE_CARRIERS)
    def test_the_onboarding_flow_ends_at_the_gate(self, carrier: str) -> None:
        """The regression is a step 8, whatever it is called.

        Asserting on the *last* numbered step rather than searching for a
        forbidden phrase is what makes this hold against a rewrite: any step
        appended after the one that labels and stops fails here, including ones
        nobody has thought of. Both files number their flow, so both get it.
        """
        text = (TEMPLATES / carrier).read_text()
        steps = [ln for ln in text.splitlines() if re.match(r"^\d+\. ", ln)]
        assert steps, f"{carrier} no longer numbers the onboarding flow"
        assert "needs:human" in steps[-1] and "STOP" in steps[-1], (
            f"{carrier}'s onboarding flow continues past the human gate: {steps[-1]}"
        )

    def test_the_agent_definition_forbids_both_ways_past_the_gate(self) -> None:
        """Closing the issue and getting on with the work are separate bypasses.

        An agent that only refuses to close it, and files the milestone's task
        issues anyway, has left the gate standing and made it decorative — the
        breakdown is already done and the human is approving a fait accompli.
        Task breakdown belongs to the orchestrator, behind its own `Milestone 1
        plan` gate, after this one clears.
        """
        rules = _section(
            (TEMPLATES / "agents" / "human_interaction.md").read_text(),
            "Onboarding (your first task)",
        )
        forbidden = [ln for ln in rules.splitlines() if re.search(r"Do \*\*NOT\*\*", ln)]
        assert any(re.search(r"close issue #1", ln, re.I) for ln in forbidden), (
            "nothing stops the onboarding agent closing the issue whose close is "
            "the human's approval"
        )
        assert any(re.search(r"task issue", ln, re.I) for ln in forbidden), (
            "nothing stops the onboarding agent breaking milestone 1 into task "
            "issues before the roadmap is approved"
        )

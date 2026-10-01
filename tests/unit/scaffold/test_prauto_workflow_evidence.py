"""Regression coverage for wf-minimal's per-pass review-evidence transport.

The generator's stage diff must reach the reviewer without travelling through a model output
stream: a large diff echoed into a completion report can be dropped (CLI output ceiling or a
safety filter), and a report missing it fail-closes the review with ESCALATE on an otherwise
correct stage (issue #116, test stage). The workflow therefore has the generator write the diff to
a file and has the reviewer read that file with its own Read tool.

spec: spec/AI_SCAFFOLD.md — "After every generator and fix pass, the parent captures complete
repository evidence"; the reviewer subagents run with Read/Glob/Grep and no shell.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
WORKFLOW = ROOT / ".claude/workflows/wf-minimal.js"
AGENT_LIB = ROOT / ".prauto/lib/agent.sh"


def test_generator_writes_the_diff_to_a_file_instead_of_echoing_it() -> None:
    """The full diff is redirected to a file, never pasted into the report.

    A diff that large is exactly the artifact a model output stream can truncate; redirecting it
    to disk is what keeps it whole for the reviewer.
    """
    source = WORKFLOW.read_text()

    assert "function evidencePath(stage)" in source, (
        "wf-minimal no longer defines the per-stage evidence path"
    )
    # The path must live where the reviewer can read it: not under .prauto/state/, which
    # DENY_TOOLS makes unreadable to reviewers.
    assert ".prauto/evidence/" in source, "the evidence path left the reviewer-readable directory"
    assert ".prauto/state/" not in source.split("function evidencePath")[1].split("}")[0], (
        "the evidence path was moved under .prauto/state/, which DENY_TOOLS denies to the reviewer"
    )
    # The generator clause redirects `git show` into that file.
    assert "git show HEAD > " in source, "the generator no longer redirects the diff to a file"
    # And it must say NOT to paste the diff into the report.
    assert "Do NOT paste the full diff" in source, (
        "the generator clause no longer forbids pasting the full diff into the report"
    )


def test_reviewer_is_pointed_at_the_evidence_file_and_fails_closed_without_it() -> None:
    """The review prompt names the file, and a missing file is still an ESCALATE."""
    source = WORKFLOW.read_text()

    review_prompt = source.split("function reviewPrompt", 1)[1].split("\n}", 1)[0]
    assert "evidencePath(stage)" in review_prompt, (
        "the review prompt no longer names the evidence file the reviewer must read"
    )
    assert re.search(r"missing|incomplete", review_prompt, re.IGNORECASE), (
        "the review prompt no longer tells the reviewer to ESCALATE on missing evidence"
    )


def test_implementation_allowlist_permits_the_commands_the_evidence_clause_names() -> None:
    """`git show` and `mkdir` are instructed, so they belong in the phase's allowlist.

    The evidence clause tells the generator to run `mkdir -p .prauto/evidence` and
    `git show HEAD`; an allowlist that omits either contradicts the prompt the generator is given.
    """
    lib = AGENT_LIB.read_text()
    line = next(
        (ln for ln in lib.splitlines() if ln.startswith("IMPLEMENTATION_ALLOWED_TOOLS=")),
        None,
    )
    assert line is not None, "IMPLEMENTATION_ALLOWED_TOOLS is no longer defined"

    assert "Bash(git show *)" in line, "the implementation allowlist omits `git show`"
    assert "Bash(mkdir *)" in line, "the implementation allowlist omits `mkdir`"
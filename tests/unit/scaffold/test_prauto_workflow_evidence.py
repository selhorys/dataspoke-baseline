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

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
WORKFLOW = ROOT / ".claude/workflows/wf-minimal.js"
AGENT_LIB = ROOT / ".prauto/lib/agent.sh"
IMPL_PROMPT = ROOT / ".prauto/prompts/implementation.md"


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
    # The generator clause redirects the diff into that file: the cumulative branch diff when a
    # base ref is known, `git show HEAD` as the no-base fallback.
    assert "...HEAD >> " in source, "the generator no longer redirects the branch diff to a file"
    assert "git show HEAD > " in source, "the no-base fallback no longer redirects to a file"
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
    """Every evidence command the clause instructs belongs in the phase's allowlist.

    The evidence clause tells the generator to run `mkdir -p .prauto/evidence`, then the
    cumulative `git log` / `git diff` capture (or the `git show HEAD` no-base fallback). A denied
    command surfaces as a missing-evidence ESCALATE that looks like a reviewer verdict, so an
    allowlist that omits any of them must fail here instead.
    """
    lib = AGENT_LIB.read_text()
    line = next(
        (ln for ln in lib.splitlines() if ln.startswith("IMPLEMENTATION_ALLOWED_TOOLS=")),
        None,
    )
    assert line is not None, "IMPLEMENTATION_ALLOWED_TOOLS is no longer defined"

    assert "Bash(git show *)" in line, "the implementation allowlist omits `git show`"
    assert "Bash(mkdir *)" in line, "the implementation allowlist omits `mkdir`"
    assert "Bash(git log *)" in line, "the implementation allowlist omits `git log`"
    assert "Bash(git diff *)" in line, "the implementation allowlist omits `git diff`"


def test_implementation_prompt_passes_the_base_ref_to_the_workflow() -> None:
    """The executor-rendered prompt hands wf-minimal the base ref the branch forked from.

    Without it the workflow falls back to last-commit evidence, which is what escalated the
    resumed runs of issues #149 and #152.
    """
    prompt = IMPL_PROMPT.read_text()
    assert '"base":' in prompt and "origin/{base_branch}" in prompt, (
        "implementation.md no longer passes wf-minimal a base ref built from {base_branch}"
    )
    # The executor must actually render that placeholder.
    assert "base_branch=${PRAUTO_BASE_BRANCH}" in AGENT_LIB.read_text(), (
        "run_implementation no longer renders {base_branch} into the implementation prompt"
    )


def _run_workflow(args: dict) -> dict:
    """Execute wf-minimal under node with stubbed harness globals; return captured prompts."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = """
const fs = require('fs')
const src = fs.readFileSync(process.argv[1], 'utf8').replace(/^export const meta/m, 'const meta')
const prompts = []
const agent = async (prompt, opts) => {
  prompts.push({ type: opts.agentType, prompt })
  return opts.schema ? { verdict: 'APPROVE', summary: 'ok', findings: [] } : 'report'
}
const parallel = async (fns) => Promise.all(fns.map(f => f()))
const log = () => {}
const run = new (Object.getPrototypeOf(async function () {}).constructor)(
  'args', 'agent', 'parallel', 'log', src)
run(JSON.parse(process.argv[2]), agent, parallel, log)
  .then(result => console.log(JSON.stringify({ result, prompts })))
  .catch(err => console.log(JSON.stringify({ error: String(err.message) })))
"""
    proc = subprocess.run(
        [node, "-e", harness, str(WORKFLOW), json.dumps(args)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout)


_BASE_ARGS = {"plan": "p", "stages": ["spec"], "security": ["spec"]}


def test_evidence_is_the_cumulative_branch_diff_when_a_base_is_given() -> None:
    """Generator and reviewers both see the merge-base diff, not just the latest commit."""
    out = _run_workflow({**_BASE_ARGS, "base": "origin/dev"})
    assert out["result"]["outcome"] == "COMPLETE"
    gen = next(p["prompt"] for p in out["prompts"] if p["type"] == "spec")
    assert "git diff origin/dev...HEAD >> .prauto/evidence/spec.diff" in gen
    assert "git log --oneline origin/dev..HEAD > .prauto/evidence/spec.diff" in gen
    assert "whether or not you committed" in gen
    assert "Leave no untracked files behind" in gen, "generators may leave uncaptured files"
    # No file name from the worktree is ever spliced into a shell command.
    assert "--no-index" not in gen and "<that-file>" not in gen
    assert "git show HEAD" not in gen

    reviews = [p["prompt"] for p in out["prompts"] if p["type"] != "spec"]
    assert {p["type"] for p in out["prompts"]} == {"spec", "spec-reviewer", "security-reviewer"}
    for review in reviews:
        assert "CUMULATIVE branch diff since the merge-base with origin/dev" in review
        assert ".prauto/evidence/spec.diff" in review
        assert "untracked" in review, "reviewers are not told to escalate on uncaptured files"


def test_evidence_falls_back_to_the_last_commit_without_a_base() -> None:
    out = _run_workflow(_BASE_ARGS)
    gen = next(p["prompt"] for p in out["prompts"] if p["type"] == "spec")
    assert "git show HEAD > .prauto/evidence/spec.diff" in gen


@pytest.mark.parametrize(
    "bad", ["origin/dev; rm -rf /", "-p", "origin/dev..HEAD", "origin dev", "a$(x)", 7]
)
def test_malformed_base_ref_is_rejected(bad: object) -> None:
    """The ref is interpolated into shell commands, so anything but a plain ref name throws."""
    out = _run_workflow({**_BASE_ARGS, "base": bad})
    assert "malformed base ref" in out.get("error", ""), out

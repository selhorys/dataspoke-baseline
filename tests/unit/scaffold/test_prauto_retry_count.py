"""Regression coverage for PRauto retry state lifecycle scoping.

The shell snippets use a temporary PRauto state directory and make no network
or GitHub calls.

spec: spec/AI_PRAUTO.md §Retry tracking and §Issue restart protocol
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"
STATE_LIBRARY = ROOT / ".prauto/lib/state.sh"
HELPERS_LIBRARY = ROOT / ".prauto/lib/helpers.sh"
ISSUE_NUMBER = "177"
FIRST_READY_TIMESTAMP = "2026-09-01T00:00:00Z"
SECOND_READY_TIMESTAMP = "2026-09-02T00:00:00Z"


def _run_bash(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a state-library snippet without sharing process or filesystem state.

    The retry and refund bounds are read from the environment, so an exported
    value in the developer's shell would otherwise silently change what these
    tests assert — passing some vacuously and failing others for the wrong reason.
    """
    child = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PRAUTO_MAX_REFUNDS_PER_JOB", "PRAUTO_MAX_RETRIES_PER_JOB"}
    }
    child.update(env or {})
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        check=False,
        text=True,
        env=child,
    )


def _source_state_library(state_root: Path, ready_timestamp: str) -> str:
    """Return shell setup for an isolated retry-state lifecycle."""
    return "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(state_root))}",
            f"READY_LABEL_TIMESTAMP={shlex.quote(ready_timestamp)}",
            f"source {shlex.quote(str(HELPERS_LIBRARY))}",
            f"source {shlex.quote(str(STATE_LIBRARY))}",
            "ensure_state_dirs",
        ]
    )


def test_retry_count_increments_only_within_the_same_ready_lifecycle(tmp_path: Path) -> None:
    """A fresh shell process preserves the count only for its ready-label lifecycle.

    spec: spec/AI_PRAUTO.md §Retry tracking — genuine attempt starts consume the
    configured retry budget; §Issue restart protocol — a re-queued issue is
    a fresh lifecycle.
    """
    state_root = tmp_path / "prauto"
    first_attempt = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert first_attempt.returncode == 0, first_attempt.stderr

    same_lifecycle = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 2',
            ]
        )
    )
    assert same_lifecycle.returncode == 0, same_lifecycle.stderr

    persisted_same_lifecycle = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 2',
            ]
        )
    )
    assert persisted_same_lifecycle.returncode == 0, persisted_same_lifecycle.stderr


def test_retry_count_resets_when_ready_label_starts_a_new_lifecycle(tmp_path: Path) -> None:
    """A re-queued issue starts from retry zero rather than old local state.

    spec: spec/AI_PRAUTO.md §Issue restart protocol — restoring
    ``prauto:ready`` starts a new job lifecycle with a fresh retry budget.
    """
    state_root = tmp_path / "prauto"
    seeded = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"increment_retry_count {ISSUE_NUMBER}",
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 2',
            ]
        )
    )
    assert seeded.returncode == 0, seeded.stderr

    requeued = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, SECOND_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert requeued.returncode == 0, requeued.stderr

    persisted_requeue = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, SECOND_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert persisted_requeue.returncode == 0, persisted_requeue.stderr


def test_retry_count_reaches_four_before_the_next_dispatch_checks_the_limit(tmp_path: Path) -> None:
    """The fourth genuine attempt is persisted for the next dispatch boundary.

    spec: spec/AI_PRAUTO.md §Retry tracking — count three advances to four and
    the following dispatch observes four before it can start another worker.
    """
    state_root = tmp_path / "prauto"
    counter_file = state_root / "state" / f"retry-count-{ISSUE_NUMBER}.json"
    counter_file.parent.mkdir(parents=True)
    counter_file.write_text(
        json.dumps(
            {
                "issue_number": int(ISSUE_NUMBER),
                "count": 3,
                "ready_label_timestamp": FIRST_READY_TIMESTAMP,
            }
        )
        + "\n"
    )
    fourth_attempt = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 3',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 4',
            ]
        )
    )
    assert fourth_attempt.returncode == 0, fourth_attempt.stderr
    next_dispatch = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 4',
            ]
        )
    )
    assert next_dispatch.returncode == 0, next_dispatch.stderr


def test_retry_count_recovers_from_missing_or_invalid_local_records(tmp_path: Path) -> None:
    """Invalid retry state never blocks or inflates the next valid attempt.

    spec: spec/AI_PRAUTO.md §Retry tracking — local recovery state is
    fail-safe and must not cause a job to be abandoned from corrupt state.
    """
    state_root = tmp_path / "prauto"
    missing = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
            ]
        )
    )
    assert missing.returncode == 0, missing.stderr

    counter_file = state_root / "state" / f"retry-count-{ISSUE_NUMBER}.json"
    counter_file.write_text("{not-json\n")
    recovered = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
                f"jq -e '.count == 1' {shlex.quote(str(counter_file))}",
            ]
        )
    )
    assert recovered.returncode == 0, recovered.stderr

    counter_file.write_text(
        '{"issue_number": 177, "count": 3, "last_updated": "2026-08-31T00:00:00Z"}\n'
    )
    legacy_recovered = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"COUNTER_FILE={shlex.quote(str(counter_file))}",
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert legacy_recovered.returncode == 0, legacy_recovered.stderr

    legacy_persisted = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert legacy_persisted.returncode == 0, legacy_persisted.stderr

    counter_file.write_text(
        '{"issue_number": 177, "count": 1.5, '
        f'"ready_label_timestamp": "{FIRST_READY_TIMESTAMP}", '
        '"last_updated": "2026-09-01T00:00:00Z"}\n'
    )
    fractional_recovered = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert fractional_recovered.returncode == 0, fractional_recovered.stderr

    fractional_persisted = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert fractional_persisted.returncode == 0, fractional_persisted.stderr

    counter_file.write_text(
        json.dumps(
            {
                "issue_number": 999,
                "count": 3,
                "ready_label_timestamp": FIRST_READY_TIMESTAMP,
            }
        )
        + "\n"
    )
    foreign_issue_recovered = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 0',
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert foreign_issue_recovered.returncode == 0, foreign_issue_recovered.stderr


def test_quota_resume_and_repause_do_not_advance_an_existing_retry_count(tmp_path: Path) -> None:
    """Heartbeat's quota-resume branch bypasses normal retry dispatch.

    spec: spec/AI_PRAUTO.md §Retry tracking — quota-pause cycles bypass the
    normal dispatch path and do not consume a retry slot; §Quota-pause and
    resume — a resumed session may pause again when quota is exhausted.
    """
    prauto_copy = tmp_path / "prauto"
    shutil.copytree(PRAUTO, prauto_copy)
    (prauto_copy / "config.local.env").write_text(
        "\n".join(
            [
                "PRAUTO_WORKER_ID=worker",
                "PRAUTO_GIT_AUTHOR_NAME=worker",
                "PRAUTO_GIT_AUTHOR_EMAIL=worker@example.invalid",
                "PRAUTO_AGENT=claude",
                "PRAUTO_OPEN_ISSUE_LIMIT=1",
                "PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY=false",
                "PRAUTO_CLUSTER_PROVISION_ENABLED=false",
                "",
            ]
        )
    )
    seeded = _run_bash(
        "\n".join(
            [
                f"PRAUTO_DIR={shlex.quote(str(prauto_copy))}",
                f"READY_LABEL_TIMESTAMP={shlex.quote(FIRST_READY_TIMESTAMP)}",
                f"source {shlex.quote(str(prauto_copy / 'lib/helpers.sh'))}",
                f"source {shlex.quote(str(prauto_copy / 'lib/state.sh'))}",
                "ensure_state_dirs",
                f"increment_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert seeded.returncode == 0, seeded.stderr

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    comments_file = tmp_path / "comments.json"
    comments_file.write_text(
        "[\n"
        '  {"createdAt":"2026-09-01T00:01:00Z","author":{"login":"worker"},'
        '"body":"prauto(worker): Plan\\n## Implementation Plan\\n- resume"},\n'
        '  {"createdAt":"2026-09-01T00:02:00Z","author":{"login":"worker"},'
        '"body":"prauto(worker): Paused — claude quota exhausted.\\n'
        'prauto:quota-paused\\nprauto:agent=claude\\nprauto:session=prior-session"},\n'
        '  {"createdAt":"2026-09-01T00:02:30Z","author":{"login":"reviewer"},'
        '"body":"go ahead"}\n'
        "]\n"
    )
    before_comments = json.loads(comments_file.read_text())
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "$1" == api && "$2" == user ]]; then printf 'worker\\n'; exit 0; fi
            if [[ "$1" == api ]]; then printf '%s\\n' "$READY_TIMESTAMP"; exit 0; fi
            if [[ "$1" == issue && "$2" == list ]]; then
              printf '%s\\n' '[{"number":177,"title":"retry","labels":[{"name":"prauto:wip"}]}]'
              exit 0
            fi
            if [[ "$1" == pr && "$2" == list ]]; then exit 0; fi
            if [[ "$1" == issue && "$2" == view ]]; then
              if [[ " $* " == *' --json labels '* ]]; then
                printf '{"labels":[{"name":"prauto:wip"}]}\\n'
              else
                cat "$GH_COMMENTS"
              fi
              exit 0
            fi
            if [[ "$1" == issue && "$2" == comment ]]; then
              while [[ "$1" != --body ]]; do shift; done; body="$2"
              jq --arg body "$body" '
                . + [{createdAt:"2026-09-01T00:03:00Z", author:{login:"worker"}, body:$body}]
              ' "$GH_COMMENTS" > "$GH_COMMENTS.tmp"
              mv "$GH_COMMENTS.tmp" "$GH_COMMENTS"
              exit 0
            fi
            exit 0
            """
        )
    )
    gh_stub.chmod(0o755)
    git_stub = bin_dir / "git"
    git_stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \" $* \" == *' worktree add '* ]]; then mkdir -p \"${@: -2:1}\"; fi\n"
    )
    git_stub.chmod(0o755)
    claude_stub = bin_dir / "claude"
    claude_stub.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \" $* \" == *' --resume '* ]]; then\n"
        "  printf '%s\\n' \"$@\" > \"$CLAUDE_RESUME_ARGS\"\n"
        "  printf '{\"is_error\":true,\"result\":\"quota exhausted\"}\\n'\n"
        "else\n"
        "  printf '{\"is_error\":false}\\n'\n"
        "fi\n"
    )
    claude_stub.chmod(0o755)

    result = _run_bash(
        shlex.quote(str(prauto_copy / "heartbeat.sh")),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_RESUME_ARGS": str(tmp_path / "claude-resume-args.txt"),
            "GH_COMMENTS": str(comments_file),
            "READY_TIMESTAMP": FIRST_READY_TIMESTAMP,
        },
    )
    assert result.returncode == 0, result.stderr
    persisted_count = _run_bash(
        "\n".join(
            [
                f"PRAUTO_DIR={shlex.quote(str(prauto_copy))}",
                f"READY_LABEL_TIMESTAMP={shlex.quote(FIRST_READY_TIMESTAMP)}",
                f"source {shlex.quote(str(prauto_copy / 'lib/helpers.sh'))}",
                f"source {shlex.quote(str(prauto_copy / 'lib/state.sh'))}",
                f"read_retry_count {ISSUE_NUMBER}",
                'test "$RETRY_COUNT" = 1',
            ]
        )
    )
    assert persisted_count.returncode == 0, persisted_count.stderr
    resume_args = (tmp_path / "claude-resume-args.txt").read_text().splitlines()
    assert "--resume" in resume_args
    assert resume_args[resume_args.index("--resume") + 1] == "prior-session"
    after_comments = json.loads(comments_file.read_text())
    new_comments = after_comments[len(before_comments) :]
    assert len(new_comments) == 2, result.stdout
    assert "Resumed" in new_comments[0]["body"]
    assert "prauto:quota-paused" in new_comments[1]["body"]
    assert "prauto:session=prior-session" in new_comments[1]["body"]


def test_normal_dispatch_waits_when_retry_state_cannot_be_persisted(tmp_path: Path) -> None:
    """A failed retry-state write prevents both heartbeat and agent dispatch.

    spec: spec/AI_PRAUTO.md §Retry tracking — normal dispatch increments before
    its heartbeat and worker invocation, and waits when that persistence fails.
    """
    prauto_copy = tmp_path / "prauto"
    shutil.copytree(PRAUTO, prauto_copy)
    (prauto_copy / "config.local.env").write_text(
        "\n".join(
            [
                "PRAUTO_WORKER_ID=worker",
                "PRAUTO_GIT_AUTHOR_NAME=worker",
                "PRAUTO_GIT_AUTHOR_EMAIL=worker@example.invalid",
                "PRAUTO_AGENT=claude",
                "PRAUTO_OPEN_ISSUE_LIMIT=1",
                "PRAUTO_GITHUB_ISSUE_FROM_ORG_MEMBERS_ONLY=false",
                "PRAUTO_CLUSTER_PROVISION_ENABLED=false",
                "",
            ]
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    comments_file = tmp_path / "comments.json"
    comments_file.write_text(
        "[\n"
        '  {"createdAt":"2026-09-01T00:01:00Z","author":{"login":"worker"},'
        '"body":"prauto(worker): Plan\\n## Implementation Plan\\n- work"},\n'
        '  {"createdAt":"2026-09-01T00:02:00Z","author":{"login":"reviewer"},'
        '"body":"go ahead"}\n'
        "]\n"
    )
    (bin_dir / "gh").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "$1" == api && "$2" == user ]]; then printf 'worker\\n'; exit 0; fi
            if [[ "$1" == api ]]; then printf '%s\\n' "$READY_TIMESTAMP"; exit 0; fi
            if [[ "$1" == issue && "$2" == list ]]; then
              printf '%s\\n' '[{"number":177,"title":"retry","labels":[{"name":"prauto:wip"}]}]'
              exit 0
            fi
            if [[ "$1" == pr && "$2" == list ]]; then exit 0; fi
            if [[ "$1" == issue && "$2" == view ]]; then
              if [[ " $* " == *' --json labels '* ]]; then
                printf '{"labels":[{"name":"prauto:wip"}]}\\n'
              else
                cat "$GH_COMMENTS"
              fi
              exit 0
            fi
            if [[ "$1" == issue && "$2" == comment ]]; then exit 97; fi
            exit 0
            """
        )
    )
    (bin_dir / "mktemp").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bin_dir / "git").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "claude").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >> \"$CLAUDE_ARGS\"\n"
        "printf '{\"is_error\":false}\\n'\n"
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    claude_args = tmp_path / "claude-args.txt"
    result = _run_bash(
        shlex.quote(str(prauto_copy / "heartbeat.sh")),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_ARGS": str(claude_args),
            "GH_COMMENTS": str(comments_file),
            "READY_TIMESTAMP": FIRST_READY_TIMESTAMP,
        },
    )
    assert result.returncode == 0, result.stderr
    assert "could not persist retry state" in result.stdout
    assert "--session-id" not in claude_args.read_text()
    assert len(json.loads(comments_file.read_text())) == 2

    counter_file = prauto_copy / "state" / f"retry-count-{ISSUE_NUMBER}.json"
    counter_file.write_text(
        json.dumps(
            {
                "issue_number": int(ISSUE_NUMBER),
                "count": 4,
                "ready_label_timestamp": FIRST_READY_TIMESTAMP,
            }
        )
        + "\n"
    )
    claude_args.unlink()
    abandoned = _run_bash(
        shlex.quote(str(prauto_copy / "heartbeat.sh")),
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_ARGS": str(claude_args),
            "GH_COMMENTS": str(comments_file),
            "READY_TIMESTAMP": FIRST_READY_TIMESTAMP,
        },
    )
    assert abandoned.returncode == 0, abandoned.stderr
    assert "exceeded max retries (4/4)" in abandoned.stdout
    assert "--session-id" not in claude_args.read_text()
    assert len(json.loads(comments_file.read_text())) == 2



def test_refund_retry_count_returns_one_attempt_and_floors_at_zero(tmp_path: Path) -> None:
    """A harness-caused attempt is handed back, and a refund can never go negative.

    spec: spec/AI_PRAUTO.md §Retry tracking — only genuine attempt starts consume
    the configured retry budget. The counter advances at dispatch, before the
    outcome is known, so an attempt that turns out not to be the worker's failure
    has to be given back rather than silently spent.
    """
    state_root = tmp_path / "prauto"
    setup = _source_state_library(state_root, FIRST_READY_TIMESTAMP)

    result = _run_bash(
        "\n".join(
            [
                setup,
                "RETRY_COUNT_CONSUMED=true",
                "PRAUTO_MAX_REFUNDS_PER_JOB=99",
                f"increment_retry_count {ISSUE_NUMBER}",
                f"increment_retry_count {ISSUE_NUMBER}",
                f"refund_retry_count {ISSUE_NUMBER}",
                'printf "after-refund=%s\\n" "$RETRY_COUNT"',
                # Two more refunds against a count of 1 must floor at zero, never
                # wrap to a negative budget that would make the job unabandonable.
                f"refund_retry_count {ISSUE_NUMBER}",
                f"refund_retry_count {ISSUE_NUMBER}",
                f"read_retry_count {ISSUE_NUMBER}",
                'printf "floored=%s\\n" "$RETRY_COUNT"',
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert "after-refund=1" in result.stdout
    assert "floored=0" in result.stdout


def test_refund_retry_count_keeps_the_ready_label_lifecycle(tmp_path: Path) -> None:
    """A refund rewrites the counter for the current lifecycle, not a new one.

    spec: spec/AI_PRAUTO.md §Retry tracking — the counter is scoped to the
    issue's current ready-label lifecycle, so a refunded record must still carry
    that timestamp or the next wake reads it as a foreign lifecycle and resets.
    """
    state_root = tmp_path / "prauto"
    setup = _source_state_library(state_root, FIRST_READY_TIMESTAMP)

    result = _run_bash(
        "\n".join(
            [
                setup,
                "RETRY_COUNT_CONSUMED=true",
                f"increment_retry_count {ISSUE_NUMBER}",
                f"increment_retry_count {ISSUE_NUMBER}",
                f"refund_retry_count {ISSUE_NUMBER}",
            ]
        )
    )
    assert result.returncode == 0, result.stderr

    record = json.loads((state_root / "state" / f"retry-count-{ISSUE_NUMBER}.json").read_text())
    assert record["count"] == 1
    assert record["ready_label_timestamp"] == FIRST_READY_TIMESTAMP


def test_refund_is_refused_when_this_dispatch_consumed_no_retry(tmp_path: Path) -> None:
    """Only the path that advanced the counter may give an attempt back.

    spec: spec/AI_PRAUTO.md §Retry tracking — the counter is incremented only in
    the normal dispatch path; the quota-pause cycles and the plan-approval path
    bypass it entirely. Those paths reach the same phase handler, so an ungated
    refund would take an attempt from an earlier dispatch's tally and hand the
    job more attempts than PRAUTO_MAX_RETRIES_PER_JOB allows.
    """
    state_root = tmp_path / "prauto"
    setup = _source_state_library(state_root, FIRST_READY_TIMESTAMP)

    result = _run_bash(
        "\n".join(
            [
                setup,
                "RETRY_COUNT_CONSUMED=true",
                f"increment_retry_count {ISSUE_NUMBER}",
                f"increment_retry_count {ISSUE_NUMBER}",
                # A plan-approval or quota-resume dispatch: no increment happened.
                "RETRY_COUNT_CONSUMED=false",
                f"refund_retry_count {ISSUE_NUMBER}",
                f"read_retry_count {ISSUE_NUMBER}",
                'printf "unchanged=%s\\n" "$RETRY_COUNT"',
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert "unchanged=2" in result.stdout


def test_refunds_are_capped_per_ready_label_lifecycle(tmp_path: Path) -> None:
    """A job cannot be made unabandonable by repeated refunds.

    spec: spec/AI_PRAUTO.md §Retry tracking — at PRAUTO_MAX_RETRIES_PER_JOB the
    issue is abandoned. That guarantee only holds if refunds are bounded: an
    unbounded refund nets every dispatch to zero, so the limit is never reached
    and the issue loops forever holding an open-issue slot.
    """
    state_root = tmp_path / "prauto"
    setup = _source_state_library(state_root, FIRST_READY_TIMESTAMP)

    # Alternate dispatch and refund far more times than the cap allows.
    cycles = "\n".join(
        f"increment_retry_count {ISSUE_NUMBER}\nrefund_retry_count {ISSUE_NUMBER}"
        for _ in range(6)
    )
    result = _run_bash(
        "\n".join(
            [
                setup,
                "RETRY_COUNT_CONSUMED=true",
                "PRAUTO_MAX_REFUNDS_PER_JOB=2",
                cycles,
                f"read_retry_count {ISSUE_NUMBER}",
                'printf "count=%s\\n" "$RETRY_COUNT"',
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    # Six dispatches, only the first two refunded: the counter still climbs, so
    # the abandonment threshold is reachable.
    assert "count=4" in result.stdout


def test_refund_count_resets_with_a_new_ready_label_lifecycle(tmp_path: Path) -> None:
    """A re-queued issue gets a fresh refund allowance, like its retry counter.

    spec: spec/AI_PRAUTO.md §Issue restart protocol — a fresh ready-label
    timestamp establishes a new lifecycle, and no tally may be inherited across
    it in either direction.
    """
    state_root = tmp_path / "prauto"

    first = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                "RETRY_COUNT_CONSUMED=true",
                "PRAUTO_MAX_REFUNDS_PER_JOB=1",
                f"increment_retry_count {ISSUE_NUMBER}",
                f"refund_retry_count {ISSUE_NUMBER}",
                f"read_refund_count {ISSUE_NUMBER}",
                'printf "first=%s\\n" "$REFUND_COUNT"',
            ]
        )
    )
    assert first.returncode == 0, first.stderr
    assert "first=1" in first.stdout

    second = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, SECOND_READY_TIMESTAMP),
                f"read_refund_count {ISSUE_NUMBER}",
                'printf "second=%s\\n" "$REFUND_COUNT"',
            ]
        )
    )
    assert second.returncode == 0, second.stderr
    assert "second=0" in second.stdout


def test_refund_cap_default_is_the_documented_two(tmp_path: Path) -> None:
    """The shipped cap, not an override, is what bounds an unattended worker.

    spec: spec/AI_PRAUTO.md §Retry tracking names PRAUTO_MAX_REFUNDS_PER_JOB's
    default as 2. Left unpinned, a default of 0 makes the refund inert and a large
    default makes the abandonment guarantee vacuous; neither shows up in a test
    that sets the value explicitly.
    """
    state_root = tmp_path / "prauto"
    cycles = "\n".join(
        f"increment_retry_count {ISSUE_NUMBER}\nrefund_retry_count {ISSUE_NUMBER}"
        for _ in range(6)
    )
    result = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                "RETRY_COUNT_CONSUMED=true",
                "unset PRAUTO_MAX_REFUNDS_PER_JOB",
                cycles,
                f"read_retry_count {ISSUE_NUMBER}",
                'printf "count=%s\\n" "$RETRY_COUNT"',
            ]
        ),
        env={"PRAUTO_MAX_REFUNDS_PER_JOB": ""},
    )

    assert result.returncode == 0, result.stderr
    # Six dispatches, the default two refunded.
    assert "count=4" in result.stdout


def test_an_invalid_refund_cap_falls_back_to_the_default(tmp_path: Path) -> None:
    """An operator typo must not silently unbound or disable the cap.

    spec: spec/AI_PRAUTO.md §Retry tracking — the cap is the only thing keeping
    the abandonment guarantee true in the presence of refunds, so a value that
    cannot be compared numerically has to degrade to the documented default
    rather than to "no cap" or to a bash arithmetic error that kills the wake.
    """
    state_root = tmp_path / "prauto"
    cycles = "\n".join(
        f"increment_retry_count {ISSUE_NUMBER}\nrefund_retry_count {ISSUE_NUMBER}"
        for _ in range(6)
    )
    result = _run_bash(
        "\n".join(
            [
                _source_state_library(state_root, FIRST_READY_TIMESTAMP),
                "RETRY_COUNT_CONSUMED=true",
                'PRAUTO_MAX_REFUNDS_PER_JOB="4h"',
                cycles,
                f"read_retry_count {ISSUE_NUMBER}",
                'printf "count=%s\\n" "$RETRY_COUNT"',
            ]
        )
    )

    assert result.returncode == 0, result.stderr
    assert "count=4" in result.stdout


def test_a_refund_that_cannot_be_persisted_leaves_the_attempt_counted(
    tmp_path: Path,
) -> None:
    """A failed refund fails safe — toward spending the attempt, not freeing it.

    spec: spec/AI_PRAUTO.md §Retry tracking — if PRauto cannot persist a
    current-lifecycle counter it does not proceed as though it had. The same
    applies in the refund direction: an unwritable record must leave the consumed
    attempt on the books, or the abandonment bound drifts upward silently.

    The lifecycle anchor stays set so the write is actually attempted; the state
    directory is made unwritable so mktemp fails inside write_retry_count.

    The second sourcing deliberately skips ``ensure_state_dirs``: that helper now
    also ``chmod 700``s STATE_DIR on every call (it holds the dev-lock token
    capability, the provisioning marker, and undelivered report bodies -- all
    decisions this worker acts on, so the directory is hardened back to the
    worker's own mode each time it is ensured). Since the state dir already
    exists from the seeding call above, re-running ``ensure_state_dirs`` here
    would silently restore write access as the directory's own owner (chmod is
    always permitted for the owner, regardless of the target's current mode)
    and defeat the read-only simulation before ``refund_retry_count`` ever ran.
    """
    state_root = tmp_path / "prauto"
    setup = _source_state_library(state_root, FIRST_READY_TIMESTAMP)

    seeded = _run_bash(
        "\n".join(
            [
                setup,
                "RETRY_COUNT_CONSUMED=true",
                f"increment_retry_count {ISSUE_NUMBER}",
                f"increment_retry_count {ISSUE_NUMBER}",
            ]
        )
    )
    assert seeded.returncode == 0, seeded.stderr

    # Same sourcing as `setup`, minus the trailing `ensure_state_dirs` call.
    setup_without_ensure = "\n".join(
        [
            f"PRAUTO_DIR={shlex.quote(str(state_root))}",
            f"READY_LABEL_TIMESTAMP={shlex.quote(FIRST_READY_TIMESTAMP)}",
            f"source {shlex.quote(str(HELPERS_LIBRARY))}",
            f"source {shlex.quote(str(STATE_LIBRARY))}",
        ]
    )

    state_dir = state_root / "state"
    original_mode = state_dir.stat().st_mode
    state_dir.chmod(0o500)  # readable and traversable, not writable
    try:
        result = _run_bash(
            "\n".join(
                [
                    setup_without_ensure,
                    "RETRY_COUNT_CONSUMED=true",
                    f"refund_retry_count {ISSUE_NUMBER} && rc=0 || rc=$?",
                    'printf "rc=%s\\n" "$rc"',
                ]
            )
        )
        assert result.returncode == 0, result.stderr
        assert "rc=0" not in result.stdout, (
            "a refund that could not be persisted reported success: "
            f"{result.stdout!r}"
        )
    finally:
        state_dir.chmod(original_mode)

    record = json.loads((state_dir / f"retry-count-{ISSUE_NUMBER}.json").read_text())
    assert record["count"] == 2
    assert record["ready_label_timestamp"] == FIRST_READY_TIMESTAMP


def test_a_refund_without_a_lifecycle_anchor_is_declined(tmp_path: Path) -> None:
    """Without the lifecycle anchor there is no record to refund against.

    spec: spec/AI_PRAUTO.md §Retry tracking — the counter is scoped to the issue's
    current ready-label lifecycle; a read that cannot identify the lifecycle
    deliberately yields zero rather than guessing.
    """
    state_root = tmp_path / "prauto"
    setup = _source_state_library(state_root, FIRST_READY_TIMESTAMP)

    result = _run_bash(
        "\n".join(
            [
                setup,
                "RETRY_COUNT_CONSUMED=true",
                f"increment_retry_count {ISSUE_NUMBER}",
                f"increment_retry_count {ISSUE_NUMBER}",
                'READY_LABEL_TIMESTAMP=""',
                f"refund_retry_count {ISSUE_NUMBER}",
            ]
        )
    )
    assert result.returncode == 0, result.stderr

    record = json.loads((state_root / "state" / f"retry-count-{ISSUE_NUMBER}.json").read_text())
    assert record["count"] == 2

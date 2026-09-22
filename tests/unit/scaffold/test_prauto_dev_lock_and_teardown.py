"""Acceptance tests for the dev-env lock protocol and teardown confirmation.

Every test here pins a behaviour that, when absent, produced a measured
production incident: a transient DNS outage wedged the dev-env lock for 9.5
hours and left a full dev stack billing for four, because a release that failed
could never be retried, a stale lock had no reclaim path, and an uninstall that
reached no cluster reported success.

The lock tests drive the REAL service, extracted from the ConfigMap in
`helm-charts/dev-peripherals/dev-lock/manifests/lock-service.yaml`, rather than a
mock of it — a mock written alongside the client would agree with the client by
construction and prove nothing about the deployed artifact.

spec: spec/TESTING.md §Integration Testing (lock protocol, steps 2 and 7)
spec: spec/AI_PRAUTO.md §Provisioning (reclaim, release, teardown confirmation)
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"
LOCK_MANIFEST = ROOT / "helm-charts/dev-peripherals/dev-lock/manifests/lock-service.yaml"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _lock_service_source() -> str:
    """The deployed script itself, spliced out of the ConfigMap block scalar."""
    for doc in yaml.safe_load_all(LOCK_MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap":
            return str(doc["data"]["lock_service.py"])
    raise AssertionError("no ConfigMap carrying lock_service.py in the manifest")


def _http(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - fixed localhost URL
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def lock_service(tmp_path: Path) -> Iterator[str]:
    """The real service on a free port. Yields its base URL."""
    script = tmp_path / "lock_service.py"
    script.write_text(_lock_service_source())
    port = _free_port()
    env = {**os.environ, "LOCK_SERVICE_PORT": str(port)}
    proc = subprocess.Popen(  # noqa: S603
        ["python3", str(script)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if _http(f"{base}/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("lock service did not become ready")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _run(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    run_env.update(env or {})
    return subprocess.run(  # noqa: S603
        ["bash", "-c", script], capture_output=True, check=False, text=True, env=run_env
    )


def _harness(state_dir: Path, env_file: Path) -> str:
    """Source dev-env.sh with the globals its callers normally provide.

    PRAUTO_DIR is set BEFORE the source line because DEV_LOCK_TOKEN_FILE is a
    top-level assignment; setting it afterwards would leave the token in the real
    repo's .prauto/state/.
    """
    return "\n".join(
        [
            "set -uo pipefail",
            'warn() { printf "WARN: %s\\n" "$*"; }',
            "info() { :; }",
            # Reported on stderr, not stdout: callers invoke the acquire with stdout
            # redirected to /dev/null, so a stdout stub would be invisible.
            'regression_blocked() { printf "BLOCKED: %s\\n" "$2" >&2; }',
            "dev_env_healthy() { return 0; }",
            f"PRAUTO_DIR={shlex.quote(str(state_dir))}",
            f"REPO_DIR={shlex.quote(str(env_file.parent))}",
            f"PRAUTO_DEV_ENV_FILE={shlex.quote(str(env_file))}",
            'PRAUTO_WORKER_ID="prauto01"',
            f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
            f"source {shlex.quote(str(PRAUTO / 'lib/dev-env.sh'))}",
        ]
    )


def _env_file(tmp_path: Path, lock_url: str, **extra: str) -> Path:
    path = tmp_path / ".env.dev"
    lines = [f"DATASPOKE_DEV_LOCK_URL={lock_url}"]
    lines += [f"{k}={v}" for k, v in extra.items()]
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# F1 — a stale lock this worker left behind must be reclaimable
# ---------------------------------------------------------------------------


def test_a_lock_this_worker_leaked_is_reclaimed_with_its_token(
    tmp_path: Path, lock_service: str
) -> None:
    """A heartbeat that dies holding the lock reclaims it on its next run.

    This is the incident: attempts 3 and 4 were both blocked by HTTP 409 from
    this same worker's own never-released lock, and the retry budget was spent
    on an obstacle the worker itself had created.

    spec: spec/AI_PRAUTO.md §Provisioning -- the executor reclaims "when the
    reported holder is exactly this worker's own" acquisition, authorised by the
    persisted token rather than by the owner name.
    """
    state = tmp_path / "state-dir"
    env_file = _env_file(tmp_path, lock_service)

    # Run one: acquire, then vanish without releasing (no traps, as under SIGKILL).
    first = _run(
        _harness(state, env_file)
        + '\nacquire_required_dev_lock 183 "full regression" >/dev/null'
        + '\nprintf "acquired=%s\\n" "$REQUIRED_LOCK_OWNER"'
    )
    assert "acquired=prauto-prauto01" in first.stdout, first.stderr
    assert _http(f"{lock_service}/lock")[1]["owner"] == "prauto-prauto01"

    # Run two: a fresh process, same persisted token, same wedged lock.
    second = _run(
        _harness(state, env_file)
        + '\nacquire_required_dev_lock 183 "full regression" >/dev/null'
        + '\nprintf "reacquired=%s\\n" "$REQUIRED_LOCK_OWNER"'
    )
    assert "reacquired=prauto-prauto01" in second.stdout, second.stdout + second.stderr
    assert "BLOCKED" not in second.stderr


def test_another_workers_live_lock_is_never_reclaimed(tmp_path: Path, lock_service: str) -> None:
    """A 409 from a different holder stays blocked, and that holder is untouched.

    The reclaim must not become a force-release aimed by name: the owner string
    is published on every GitHub comment and is identical between two workers
    sharing PRAUTO_WORKER_ID, so a name match cannot distinguish a leaked lock
    from a sibling's live one.

    spec: spec/AI_PRAUTO.md §Provisioning -- "another worker's lock is never
    touched this way".
    """
    state = tmp_path / "state-dir"
    env_file = _env_file(tmp_path, lock_service)

    # This worker has a token on disk from an earlier acquisition...
    _run(_harness(state, env_file) + '\nacquire_required_dev_lock 1 "x" >/dev/null')
    _http(f"{lock_service}/lock", "DELETE")
    # ...but a live peer now holds the lock.
    assert (
        _http(f"{lock_service}/lock/acquire", "POST", {"owner": "prauto-peer", "message": "live"})[
            0
        ]
        == 200
    )

    result = _run(
        _harness(state, env_file)
        + '\nacquire_required_dev_lock 183 "full regression" >/dev/null'
        + '\nprintf "owner=[%s]\\n" "$REQUIRED_LOCK_OWNER"'
    )
    assert "owner=[]" in result.stdout, result.stdout
    assert "BLOCKED" in result.stderr, result.stderr
    # The peer still holds exactly what it held.
    assert _http(f"{lock_service}/lock")[1]["owner"] == "prauto-peer"


def test_a_409_never_discloses_the_holders_token(lock_service: str) -> None:
    """The token is minted to the acquirer alone.

    If a 409 disclosed it, any caller could take it and release a live holder --
    the reclaim would be back to something a bystander can aim.

    spec: spec/TESTING.md §Integration Testing step 2 -- a 409 "reports `owner`,
    `acquired_at`, and `message`, but never a token".
    """
    assert _http(f"{lock_service}/lock/acquire", "POST", {"owner": "alice"})[0] == 200
    status, body = _http(f"{lock_service}/lock/acquire", "POST", {"owner": "bob"})
    assert status == 409
    assert "token" not in body
    assert "token" not in _http(f"{lock_service}/lock")[1]


# ---------------------------------------------------------------------------
# F3 — deletion must be confirmed, never inferred from an exit code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kubectl_body", "env_extra", "case"),
    [
        (
            'echo "Unable to connect to the server: dial tcp: i/o timeout" >&2; exit 1',
            {"DATASPOKE_KUBE_CLUSTER": "ctx", "DATASPOKE_KUBE_DATASPOKE_NAMESPACE": "dataspoke-01"},
            "unreachable cluster",
        ),
        (
            'echo "namespace/dataspoke-01"; exit 0',
            {"DATASPOKE_KUBE_CLUSTER": "ctx", "DATASPOKE_KUBE_DATASPOKE_NAMESPACE": "dataspoke-01"},
            "namespace still present",
        ),
        (
            "exit 0",
            {"DATASPOKE_KUBE_DATASPOKE_NAMESPACE": "dataspoke-01"},
            "no cluster context in the env file",
        ),
        ("exit 0", {"DATASPOKE_KUBE_CLUSTER": "ctx"}, "env file names no namespaces"),
        (
            "exit 0",
            {
                "DATASPOKE_KUBE_CLUSTER": "ctx",
                "DATASPOKE_KUBE_DATASPOKE_NAMESPACE": "--selector=x=1",
            },
            "namespace value is a kubectl option, not a DNS-1123 label",
        ),
    ],
)
def test_namespace_probe_fails_closed(
    tmp_path: Path, kubectl_body: str, env_extra: dict[str, str], case: str
) -> None:
    """Only a clean answer from the right API server counts as proof of deletion.

    Every other outcome -- unreachable, unresolvable, unanswerable, or malformed
    input -- is "could not ask", which must never read as "absent". Treating
    those as absence is precisely what deleted the durable marker and left a
    cluster running.

    spec: spec/AI_PRAUTO.md §Provisioning -- the marker is cleared "only once the
    executor has confirmed the dev namespaces are actually gone".
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "kubectl").write_text(f"#!/usr/bin/env bash\n{kubectl_body}\n")
    (bindir / "kubectl").chmod(0o755)
    env_file = _env_file(tmp_path, "http://unused.invalid", **env_extra)

    result = _run(
        "\n".join(
            [
                _harness(tmp_path / "state-dir", env_file),
                f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
                f"if dev_env_namespaces_absent {shlex.quote(str(env_file))}; then",
                '  printf "verdict=absent\\n"',
                "else",
                '  printf "verdict=no-evidence\\n"',
                "fi",
            ]
        ),
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert "verdict=no-evidence" in result.stdout, f"{case}: {result.stdout}{result.stderr}"


def test_namespace_probe_reports_absent_only_on_a_clean_notfound(tmp_path: Path) -> None:
    """The positive case: a reachable cluster that reports nothing still there.

    Without this, the parametrised fail-closed tests above would also pass on a
    function that never returns success at all.

    spec: spec/AI_PRAUTO.md §Provisioning.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # --ignore-not-found: absent prints nothing and exits 0.
    (bindir / "kubectl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bindir / "kubectl").chmod(0o755)
    env_file = _env_file(
        tmp_path,
        "http://unused.invalid",
        DATASPOKE_KUBE_CLUSTER="ctx",
        DATASPOKE_KUBE_DATASPOKE_NAMESPACE="dataspoke-01",
    )

    result = _run(
        "\n".join(
            [
                _harness(tmp_path / "state-dir", env_file),
                f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
                f"if dev_env_namespaces_absent {shlex.quote(str(env_file))}; then",
                '  printf "verdict=absent\\n"',
                "else",
                '  printf "verdict=no-evidence\\n"',
                "fi",
            ]
        ),
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert "verdict=absent" in result.stdout, result.stdout + result.stderr


def test_an_uninstall_that_deletes_nothing_does_not_clear_the_marker(
    tmp_path: Path,
) -> None:
    """The core F3 regression: exit 0 is a claim, not evidence.

    The measured incident ran an uninstall against an unreachable cluster; every
    probe reported "does not exist -- skipping" and the script exited 0. The
    caller cleared the durable marker, which is the only trigger
    recover_orphaned_dev_env has, and the stack ran on for hours.

    spec: spec/AI_PRAUTO.md §Provisioning -- "A zero exit from `uninstall.sh` is
    not itself proof of deletion".
    """
    repo = tmp_path / "repo"
    (repo / "helm-charts/bin").mkdir(parents=True)
    # A stub that lies exactly the way the real script did: success, no deletions.
    stub = repo / "helm-charts/bin/uninstall.sh"
    stub.write_text('#!/usr/bin/env bash\necho "Namespace does not exist - skipping."\nexit 0\n')
    stub.chmod(0o755)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "kubectl").write_text('#!/usr/bin/env bash\necho "namespace/dataspoke-01"\nexit 0\n')
    (bindir / "kubectl").chmod(0o755)

    env_file = _env_file(
        repo,
        "http://unused.invalid",
        DATASPOKE_KUBE_CLUSTER="ctx",
        DATASPOKE_KUBE_DATASPOKE_NAMESPACE="dataspoke-01",
    )
    state = tmp_path / "state-dir"
    marker = state / "state/dev-env-provisioned.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"env_file": str(env_file)}))

    result = _run(
        "\n".join(
            [
                "set -uo pipefail",
                'warn() { printf "WARN: %s\\n" "$*"; }',
                'info() { printf "INFO: %s\\n" "$*"; }',
                f"PRAUTO_DIR={shlex.quote(str(state))}",
                f"REPO_DIR={shlex.quote(str(repo))}",
                f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
                f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
                f"source {shlex.quote(str(PRAUTO / 'lib/dev-env.sh'))}",
                f"DEV_ENV_STATE_FILE={shlex.quote(str(marker))}",
                "DEV_ENV_PROVISIONED=true",
                f"DEV_ENV_PROVISIONED_ENV_FILE={shlex.quote(str(env_file))}",
                "DEV_ENV_TEARDOWN_ATTEMPTED=false",
                "teardown_provisioned_dev_env",
            ]
        ),
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert marker.exists(), (
        f"the durable marker was deleted on an unverified teardown: {result.stdout}{result.stderr}"
    )
    assert "Provisioned dev cluster torn down." not in result.stdout


def test_a_marker_naming_a_foreign_env_file_is_discarded_untouched(
    tmp_path: Path,
) -> None:
    """The marker aims a --delete-all teardown, so it is checked before it is used.

    Nothing should be able to point a non-interactive, PVC-and-namespace-deleting
    teardown at an arbitrary env file -- a prod one above all.

    spec: spec/AI_PRAUTO.md §Provisioning.
    """
    repo = tmp_path / "repo"
    (repo / "helm-charts/bin").mkdir(parents=True)
    ran = repo / "uninstall-ran.txt"
    stub = repo / "helm-charts/bin/uninstall.sh"
    stub.write_text(f"#!/usr/bin/env bash\ntouch {shlex.quote(str(ran))}\nexit 0\n")
    stub.chmod(0o755)

    _env_file(repo, "http://unused.invalid", DATASPOKE_KUBE_CLUSTER="ctx")
    foreign = tmp_path / "elsewhere.env"
    foreign.write_text("DATASPOKE_KUBE_CLUSTER=prod\n")

    state = tmp_path / "state-dir"
    marker = state / "state/dev-env-provisioned.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"env_file": str(foreign)}))

    result = _run(
        "\n".join(
            [
                "set -uo pipefail",
                'warn() { printf "WARN: %s\\n" "$*"; }',
                "info() { :; }",
                f"PRAUTO_DIR={shlex.quote(str(state))}",
                f"REPO_DIR={shlex.quote(str(repo))}",
                'PRAUTO_DEV_ENV_FILE="helm-charts/.env.dev"',
                f"source {shlex.quote(str(PRAUTO / 'lib/helpers.sh'))}",
                f"source {shlex.quote(str(PRAUTO / 'lib/quota.sh'))}",
                f"source {shlex.quote(str(PRAUTO / 'lib/dev-env.sh'))}",
                f"DEV_ENV_STATE_FILE={shlex.quote(str(marker))}",
                "recover_orphaned_dev_env",
            ]
        )
    )
    assert not ran.exists(), "a teardown was aimed at an env file this worker does not own"
    assert not marker.exists(), "the rejected marker should not survive to be retried forever"
    assert "unexpected env file" in (result.stdout + result.stderr), result.stdout + result.stderr


# ---------------------------------------------------------------------------
# F7 — an abandonment must say what actually stopped the job
# ---------------------------------------------------------------------------


def test_blocked_reasons_survive_into_a_later_process(tmp_path: Path) -> None:
    """Reasons are read by a heartbeat that did not record them.

    Abandonment happens on a later wake, in a different process, so a shell
    variable cannot carry this. Without durable reasons, an issue the
    infrastructure never let start is indistinguishable in its record from one
    whose code failed four times -- which is what the incident's abandonment
    looked like.

    spec: spec/AI_PRAUTO.md §Deterministic environmental-flake exception -- the
    reasons are carried "into its abandonment record and the GitHub abandonment
    comment".
    """
    state = tmp_path / "state-dir"
    (state / "state").mkdir(parents=True)
    prelude = "\n".join(
        [
            "set -uo pipefail",
            'warn() { printf "WARN: %s\\n" "$*"; }',
            "info() { :; }",
            f"PRAUTO_DIR={shlex.quote(str(state))}",
            'READY_LABEL_TIMESTAMP="2026-09-22T00:00:00Z"',
            f"source {shlex.quote(str(PRAUTO / 'lib/state.sh'))}",
        ]
    )
    writer = _run(
        prelude
        + '\nrecord_blocked_reason 183 "dev-env lock acquisition returned HTTP 409"'
        + '\nrecord_blocked_reason 183 "dev-env lock acquisition returned HTTP 409"'
        + '\nrecord_blocked_reason 183 "API deploy could not reach the development environment"'
    )
    assert writer.returncode == 0, writer.stderr

    reader = _run(prelude + '\nprintf "reasons=%s\\n" "$(read_blocked_reasons 183)"')
    line = next(ln for ln in reader.stdout.splitlines() if ln.startswith("reasons="))
    assert "HTTP 409" in line
    assert "API deploy" in line
    # Deduplicated: the same block recorded twice is one reason, not two.
    assert line.count("HTTP 409") == 1

    # A re-queued issue starts a new lifecycle and must not inherit them.
    other = _run(
        prelude.replace("2026-09-22T00:00:00Z", "2026-10-01T00:00:00Z")
        + '\nprintf "reasons=[%s]\\n" "$(read_blocked_reasons 183)"'
    )
    assert "reasons=[]" in other.stdout, other.stdout

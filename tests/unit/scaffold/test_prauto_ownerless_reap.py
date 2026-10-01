"""Hermetic tests for prauto's ownerless-idle-cluster reaper and its marker recovery.

Every invariant here is derived from spec/AI_PRAUTO.md §Provisioning, not from the
shell's current behaviour. Each test cites the paragraph/bullet it traces to, using these
tokens for the §Provisioning paragraphs: [reaped] "Ownerless idle clusters are reaped.",
[re-gated] "Reap markers are re-gated, never retried blindly.", [gates] "Reapability
gates.", [teardown] "Teardown verifies deletion before clearing its marker.":

* "Ownerless idle clusters are reaped." — the six conditions that must ALL hold
  (1 threshold > 0; 2 no marker and no cluster use this wake; 3 cluster answers and dev
  namespaces present; 4 dev-env lock acquired token-based and held through the uninstall;
  5 newest Helm update older than the threshold; 6 no live ``dataspoke.io/keep-until``
  pin), the fail-closed rule ("any probe that fails, times out, or is ambiguous means do
  nothing"), the marker tagged as a reap before the uninstall, uninstall output in a
  private teardown log, and "discards its local lock token state rather than releasing"
  after confirmed deletion.
* "Reap markers are re-gated, never retried blindly." — marker kind (``provision`` /
  ``reap``; no kind reads as provision; unknown kind is handled as reap) and the bullets
  Gates, Conclusive change (drop), Undetermined (keep), Already gone, Lockless retry.
* "Reapability gates." — the env-file gate, the DataSpoke-namespace anchor, the token proof
  through the API-server service proxy, and the heartbeat exclusions (heartbeat lock held;
  no signal-driven cleanup).
* "Teardown verifies deletion before clearing its marker." — the marker is cleared only
  once the namespaces are confirmed gone.
* §Per-worker dedicated cluster — the env file is the one bound by
  ``PRAUTO_DEV_ENV_FILE``, resolved under ``$REPO_DIR``.

Assertions the spec text does NOT state are labelled "impl-defined (fail-closed)" in the
owning test, with a one-line reason. They are: invalid (non-numeric) thresholds; a fresh
reap seeing no Helm releases; releasing the lock we took when a later check vetoes the
reap or the token proof fails; kubectl-option-shaped namespace values; an empty worker id;
and cleanup ordering relative to this wake's own dev-env lock release / the heartbeat-lock
release.

The cluster, Helm, the dev-lock HTTP service and ``uninstall.sh`` are fakes on PATH /
under a tmp ``REPO_DIR``. All of them record their argv to one JSONL file so a test can
assert "no uninstall", "no lock contact" and ordering. Nothing here touches a real
cluster, the network, or the real ``.prauto/state/``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[3]
PRAUTO = ROOT / ".prauto"

DS_NS = "dataspoke-01"
DATAHUB_NS = "datahub-01"
LANGFUSE_NS = "langfuse-01"
DUMMY_NS = "dummy-data-01"
ALL_NS = (DS_NS, DATAHUB_NS, LANGFUSE_NS, DUMMY_NS)
CLUSTER = "dev-ctx"
LOCK_URL = "http://lock.test:9221"
TOKEN = "tok-abc123"
SECRET = "SECRET-UNINSTALL-OUTPUT-10.1.2.3"
THRESHOLD = 7200
DAY = 86400

# --------------------------------------------------------------------------------------
# The fake tools. One python program behind four tiny wrappers; world state is one JSON
# file the test writes, and every invocation appends one line to a shared JSONL log.
# --------------------------------------------------------------------------------------

FAKE_TOOL = r"""
import json, os, sys, time
from pathlib import Path

WORLD = Path(os.environ["FAKE_WORLD"])
CALLS = Path(os.environ["FAKE_CALLS"])
COUNTERS = Path(os.environ["FAKE_COUNTERS"])


def log(**kw):
    with CALLS.open("a") as fh:
        fh.write(json.dumps(kw) + "\n")


def load():
    return json.loads(WORLD.read_text())


def fault(w, op):
    c = json.loads(COUNTERS.read_text()) if COUNTERS.exists() else {}
    c[op] = c.get(op, 0) + 1
    COUNTERS.write_text(json.dumps(c))
    # A per-call world mutation (applied BEFORE this call answers): lets a test change the
    # cluster between two probes of the same kind, e.g. a namespace recreated mid-retry.
    mut = w.get("mutations", {}).get(op, {}).get(str(c[op]))
    if mut:
        for ns, created in mut.get("namespace_created", {}).items():
            w["namespaces"][ns]["created"] = created
        WORLD.write_text(json.dumps(w))
    spec = w.get("faults", {}).get(op)
    if not spec:
        return
    calls = spec.get("calls", "all")
    if calls == "all" or c[op] in calls:
        if spec["mode"] == "hang":
            time.sleep(60)
        sys.exit(1)


def kubectl(argv, w):
    ctx, rest, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--context":
            ctx = argv[i + 1]; i += 2
        elif a.startswith("--request-timeout"):
            i += 1
        else:
            rest.append(a); i += 1
    if ctx != w["cluster"]:
        sys.stderr.write("wrong or missing --context\n")
        sys.exit(1)
    if rest[:2] == ["get", "namespace"]:
        fault(w, "kubectl.get_namespace")
        names, fmt, i = [], None, 2
        while i < len(rest):
            if rest[i] == "-o":
                fmt = rest[i + 1]; i += 2
            elif rest[i].startswith("-"):
                i += 1
            else:
                names.append(rest[i]); i += 1
        present = [n for n in names if n in w["namespaces"]]
        if fmt == "name":
            for n in present:
                print("namespace/" + n)
        elif fmt == "json" and present:
            def obj(n):
                meta = {"name": n, "creationTimestamp": w["namespaces"][n]["created"]}
                ann = w["namespaces"][n].get("annotations") or {}
                if ann:
                    meta["annotations"] = ann
                return {"kind": "Namespace", "metadata": meta}
            if len(present) == 1:
                print(json.dumps(obj(present[0])))
            else:
                print(json.dumps({"kind": "List", "items": [obj(n) for n in present]}))
        return
    if rest[:2] == ["get", "service"]:
        fault(w, "kubectl.get_service")
        ns = rest[rest.index("-n") + 1]
        if w.get("dev_lock_service", True) and ns in w["namespaces"]:
            print("service/" + rest[2])
        return
    if rest[:2] == ["create", "--raw"]:
        fault(w, "kubectl.create_raw")
        body = Path(rest[rest.index("-f") + 1]).read_text()
        log(tool="kubectl-body", path=rest[2], body=body)
        owner = json.loads(body).get("owner")
        mode = w.get("proxy", "ok")
        if mode == "fail":
            sys.exit(1)
        if mode == "other_owner":
            print(json.dumps({"locked": True, "owner": "someone-else"}))
        elif mode == "garbage":
            print("not json")
        else:
            print(json.dumps({"locked": True, "owner": owner}))
        return
    sys.stderr.write("unsupported kubectl invocation\n")
    sys.exit(2)


def helm(argv, w):
    ctx = argv[argv.index("--kube-context") + 1]
    if ctx != w["cluster"]:
        sys.exit(1)
    ns = argv[argv.index("-n") + 1]
    fault(w, "helm.list")
    print(json.dumps(w["helm"].get(ns, [])))


def curl(argv, w):
    url = next((a for a in argv if a.startswith("http")), "")
    method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
    body = sys.stdin.read() if "--data-binary" in argv else ""
    log(tool="curl-detail", method=method, url=url, body=body)
    lock = w["lock"]
    if not url.startswith(lock["url"]):
        sys.exit(7)
    if url.endswith("/health"):
        sys.exit(0 if lock.get("health", True) else 22)
    if method != "POST":
        sys.exit(22)
    if url.endswith("/acquire"):
        mode = lock.get("acquire", 200)
        if mode == "fail":
            sys.exit(7)
        if mode == 200:
            print(json.dumps({"token": os.environ["FAKE_TOKEN"]}))
        else:
            print(json.dumps({"error": "locked"}))
        print(mode, end="")
        return
    if url.endswith("/release"):
        print(lock.get("release", 200), end="")
        return
    if url.endswith("/renew"):
        print(200, end="")
        return
    sys.exit(22)


def uninstall(argv, w):
    marker = os.environ.get("FAKE_MARKER", "")
    content = Path(marker).read_text() if marker and Path(marker).exists() else None
    log(tool="uninstall-detail", marker=content)
    print(os.environ["FAKE_SECRET"])
    sys.stderr.write(os.environ["FAKE_SECRET"] + "\n")
    spec = w.get("uninstall", {})
    if spec.get("deletes", True) and spec.get("exit", 0) == 0:
        w["namespaces"] = {}
        w["dev_lock_service"] = False
    for k, v in (spec.get("set_faults") or {}).items():
        w.setdefault("faults", {})[k] = v
    WORLD.write_text(json.dumps(w))
    sys.exit(spec.get("exit", 0))


def health_check(argv, w):
    sys.exit(w.get("health_exit", 0))


def main():
    tool, argv = sys.argv[1], sys.argv[2:]
    log(tool=tool, argv=argv)
    w = load()
    handlers = {
        "kubectl": kubectl,
        "helm": helm,
        "curl": curl,
        "uninstall": uninstall,
        "health-check": health_check,
    }
    handlers[tool](argv, w)


main()
"""


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _helm_updated(epoch: float) -> str:
    """Helm's own rendering: local wall-clock, numeric offset, zone name."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M:%S.123456 +0000 UTC")


def _env_text(*, lock_url: str | None = LOCK_URL, extra: tuple[str, ...] = ()) -> str:
    lines = [
        f"DATASPOKE_KUBE_CLUSTER={CLUSTER}",
        f"DATASPOKE_KUBE_DATASPOKE_NAMESPACE={DS_NS}",
        f"DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE={DATAHUB_NS}",
        f"DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE={LANGFUSE_NS}",
        f"DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE={DUMMY_NS}",
    ]
    if lock_url is not None:
        lines.append(f"DATASPOKE_DEV_LOCK_URL={lock_url}")
    lines.extend(extra)
    return "\n".join(lines) + "\n"


class Sandbox:
    """A fake cluster + lock service + uninstall.sh + a scratch PRAUTO_DIR/REPO_DIR."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.prauto = root / "prauto"
        self.bin = root / "bin"
        self.world_file = root / "world.json"
        self.calls_file = root / "calls.jsonl"
        self.counters_file = root / "counters.json"
        self.env_rel = "helm-charts/.env.dev"
        self.env_file = self.repo / self.env_rel
        self.marker = self.prauto / "state" / "dev-env-provisioned.json"
        self.token_file = self.prauto / "state" / "dev-lock-token.json"
        self.env: dict[str, str | None] = {"PRAUTO_DEV_ENV_REAP_IDLE_SECS": str(THRESHOLD)}
        self.now = int(time.time())
        self.last: subprocess.CompletedProcess[str] | None = None

        (self.repo / "helm-charts/bin").mkdir(parents=True)
        self.bin.mkdir()
        self.prauto.joinpath("state").mkdir(parents=True)
        tool = root / "fake_tool.py"
        tool.write_text(FAKE_TOOL)
        for name in ("kubectl", "helm", "curl"):
            self._wrapper(self.bin / name, name, tool)
        self._wrapper(self.repo / "helm-charts/bin/uninstall.sh", "uninstall", tool)
        # run_with_timeout polls its child with `sleep 1`, which makes every probe cost
        # at least a second. Poll faster; every other duration is untouched, so timeouts
        # (deadline arithmetic on `date +%s`) and the lock lease sleeps stay real.
        fast_sleep = self.bin / "sleep"
        fast_sleep.write_text(
            '#!/usr/bin/env bash\n[[ "$1" == 1 ]] && set -- 0.05\nexec /bin/sleep "$@"\n'
        )
        fast_sleep.chmod(fast_sleep.stat().st_mode | stat.S_IXUSR)
        # health-check.sh must never run: running it would be "touching the cluster".
        self._wrapper(self.repo / "helm-charts/bin/health-check.sh", "health-check", tool)
        self.env_file.write_text(_env_text())

        self.world: dict[str, Any] = {
            "cluster": CLUSTER,
            "namespaces": {
                n: {"created": _iso(self.now - 10 * DAY), "annotations": {}} for n in ALL_NS
            },
            "dev_lock_service": True,
            "helm": {
                DATAHUB_NS: [{"name": "datahub", "updated": _helm_updated(self.now - 3 * DAY)}],
                DS_NS: [{"name": "dataspoke", "updated": _helm_updated(self.now - 3 * DAY)}],
                LANGFUSE_NS: [],
                DUMMY_NS: [],
            },
            "lock": {"url": LOCK_URL, "health": True, "acquire": 200, "release": 200},
            "proxy": "ok",
            "uninstall": {"exit": 0, "deletes": True},
            "faults": {},
        }

    @staticmethod
    def _wrapper(path: Path, name: str, tool: Path) -> None:
        path.write_text(f'#!/usr/bin/env bash\nexec python3 {shlex.quote(str(tool))} {name} "$@"\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    # -- world mutation helpers ---------------------------------------------------------

    def helm_age(self, ns: str, seconds_ago: float) -> None:
        self.world["helm"][ns] = [{"name": "rel", "updated": _helm_updated(self.now - seconds_ago)}]

    def annotate_ds(self, value: str) -> None:
        self.world["namespaces"][DS_NS]["annotations"] = {"dataspoke.io/keep-until": value}

    def fault(self, op: str, *, mode: str = "fail", calls: str | list[int] = "all") -> None:
        self.world["faults"][op] = {"mode": mode, "calls": calls}

    def mutate_on_call(self, op: str, call: int, *, namespace_created: dict[str, str]) -> None:
        """Before the `call`-th invocation of `op` answers, re-create the given namespaces."""
        self.world.setdefault("mutations", {}).setdefault(op, {})[str(call)] = {
            "namespace_created": namespace_created
        }

    def write_env(self, text: str, *, rel: str | None = None) -> None:
        if rel is not None:
            self.env_rel = rel
            self.env_file = self.repo / rel
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.env_file.write_text(text)

    def write_marker(
        self, kind: str | None, *, age: int = 1000, provisioned_at: str | None = None
    ) -> None:
        body: dict[str, str] = {"env_file": str(self.env_file)}
        if kind is not None:
            body["kind"] = kind
        body["provisioned_at"] = _iso(self.now - age) if provisioned_at is None else provisioned_at
        self.marker.write_text(json.dumps(body))

    # -- running + reading the log ------------------------------------------------------

    def run(self, body: str) -> subprocess.CompletedProcess[str]:
        self.world_file.write_text(json.dumps(self.world))
        # Test-only hook: PRAUTO_TEST_LIB_DIR points the harness at a COPY of .prauto/lib, so a
        # mutation of the library (see the lockless second-pass tests) never touches the repo.
        lib = Path(os.environ.get("PRAUTO_TEST_LIB_DIR", PRAUTO / "lib"))
        script = "\n".join(
            [
                "set -euo pipefail",
                f"PRAUTO_DIR={shlex.quote(str(self.prauto))}",
                f"REPO_DIR={shlex.quote(str(self.repo))}",
                "PRAUTO_WORKER_ID=tester",
                f"PRAUTO_DEV_ENV_FILE={shlex.quote(self.env_rel)}",
                f"source {shlex.quote(str(lib / 'helpers.sh'))}",
                f"source {shlex.quote(str(lib / 'quota.sh'))}",
                f"source {shlex.quote(str(lib / 'phases.sh'))}",
                body,
            ]
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PRAUTO_", "DATASPOKE_", "FAKE_", "DEV_"))
        }
        env.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_WORLD": str(self.world_file),
                "FAKE_CALLS": str(self.calls_file),
                "FAKE_COUNTERS": str(self.counters_file),
                "FAKE_MARKER": str(self.marker),
                "FAKE_TOKEN": TOKEN,
                "FAKE_SECRET": SECRET,
                # Keep every wait short and every retry single-shot.
                "PRAUTO_DEV_ENV_NS_PROBE_TIMEOUT_SECS": "5",
                "PRAUTO_DEV_LOCK_RENEW_SECS": "3600",
                "PRAUTO_DEV_LOCK_RENEWER_STOP_SECS": "2",
                "PRAUTO_DEV_LOCK_RELEASE_ATTEMPTS": "1",
                "PRAUTO_DEV_LOCK_RELEASE_RETRY_SECS": "0",
            }
        )
        for key, value in self.env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        self.last = subprocess.run(  # noqa: S603
            ["bash", "-c", script],
            capture_output=True,
            check=False,
            text=True,
            env=env,
            timeout=180,
        )
        return self.last

    def reap(self) -> subprocess.CompletedProcess[str]:
        # Bare under `set -e`, exactly as heartbeat.sh's EXIT trap calls it: the
        # function's contract is that it never returns non-zero.
        return self.run("reap_ownerless_idle_dev_env\necho REAP_RETURNED")

    def recover(self) -> subprocess.CompletedProcess[str]:
        return self.run("recover_orphaned_dev_env\necho RECOVER_RETURNED")

    def block_marker_writes(self) -> None:
        """Make the atomic `mv` onto the marker path fail (and nothing else)."""
        stub = self.bin / "mv"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do last="$a"; done\n'
            '[[ "$last" == *dev-env-provisioned.json ]] && exit 1\n'
            'exec /bin/mv "$@"\n'
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

    @property
    def combined(self) -> str:
        assert self.last is not None
        return self.last.stdout + self.last.stderr

    @property
    def output(self) -> str:
        assert self.last is not None
        return (
            f"rc={self.last.returncode}\nstdout:\n{self.last.stdout}\nstderr:\n{self.last.stderr}"
        )

    def log(self) -> list[dict[str, Any]]:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines() if line]

    def tool_calls(self, tool: str) -> list[dict[str, Any]]:
        return [c for c in self.log() if c["tool"] == tool]

    def lock_requests(self) -> list[dict[str, Any]]:
        return self.tool_calls("curl-detail")

    def lock_paths(self, suffix: str) -> list[dict[str, Any]]:
        return [c for c in self.lock_requests() if c["url"].endswith(suffix)]

    def marker_json(self) -> dict[str, Any]:
        return json.loads(self.marker.read_text())

    def index_of(self, predicate: Callable[[dict[str, Any]], bool]) -> list[int]:
        return [i for i, c in enumerate(self.log()) if predicate(c)]


@pytest.fixture
def sb(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _expected_uninstall_argv(sb: Sandbox) -> list[str]:
    return ["--profile", "dev", "--env-file", str(sb.env_file), "--no-question", "--delete-all"]


def _assert_reap_returned(sb: Sandbox, marker: str = "REAP_RETURNED") -> None:
    assert sb.last is not None
    assert sb.last.returncode == 0, sb.output
    assert marker in sb.last.stdout, sb.output


def _assert_not_reaped(sb: Sandbox) -> None:
    _assert_reap_returned(sb)
    assert sb.tool_calls("uninstall") == [], sb.output
    assert not sb.marker.exists(), "a reap marker was written for a skipped reap\n" + sb.output
    assert sb.tool_calls("health-check") == [], "the reaper must never run the health check"
    assert not any(c["method"] == "DELETE" for c in sb.lock_requests()), (
        "the reaper must never use the operator's DELETE /lock force-release"
    )


# ======================================================================================
# Positive control + the reap contract
# ======================================================================================


def test_reaps_an_ownerless_idle_cluster_when_every_condition_holds(sb: Sandbox) -> None:
    """spec: [reaped] — all six conditions; "Before the uninstall it writes the same durable
    marker as provisioning, tagged as a reap"; "the uninstall and the namespace-absence
    confirmation are identical to a provisioned cluster's"; "after confirmed deletion the
    reaper discards its local lock token state rather than releasing"; "holding it from the
    final checks through the uninstall"; [gates] (token proof via the service proxy).
    """
    started = int(time.time())
    sb.reap()
    _assert_reap_returned(sb)

    uninstalls = sb.tool_calls("uninstall")
    assert len(uninstalls) == 1, sb.output
    assert uninstalls[0]["argv"] == _expected_uninstall_argv(sb)

    # The marker already existed, tagged as a reap, when uninstall.sh ran.
    detail = sb.tool_calls("uninstall-detail")
    assert len(detail) == 1 and detail[0]["marker"] is not None, (
        "no marker on disk when the uninstall started\n" + sb.output
    )
    marker_at_uninstall = json.loads(detail[0]["marker"])
    assert marker_at_uninstall["kind"] == "reap"
    assert marker_at_uninstall["env_file"] == str(sb.env_file)
    written = datetime.strptime(marker_at_uninstall["provisioned_at"], "%Y-%m-%dT%H:%M:%SZ")
    written_epoch = written.replace(tzinfo=UTC).timestamp()
    assert started - 2 <= written_epoch <= time.time() + 2

    # Deletion really happened in the fake cluster, and only then was the marker cleared.
    assert sb.world_file.exists()
    assert json.loads(sb.world_file.read_text())["namespaces"] == {}
    assert not sb.marker.exists(), "marker must be cleared once deletion is confirmed\n" + sb.output
    uninstall_idx = sb.index_of(lambda c: c["tool"] == "uninstall")[0]
    confirmations = sb.index_of(
        lambda c: c["tool"] == "kubectl" and c["argv"][2:4] == ["get", "namespace"]
    )
    assert any(i > uninstall_idx for i in confirmations), (
        "the namespaces were never re-read after the uninstall\n" + sb.output
    )

    # Lock: normal token-based acquire as prauto-<worker>, held through the uninstall,
    # proven against the target cluster with the minted token, discarded not released.
    acquires = sb.lock_paths("/lock/acquire")
    assert len(acquires) == 1
    assert json.loads(acquires[0]["body"])["owner"] == "prauto-tester"
    assert all(c["url"].startswith(LOCK_URL) for c in sb.lock_requests())
    proofs = sb.tool_calls("kubectl-body")
    assert len(proofs) == 1
    assert json.loads(proofs[0]["body"]) == {"owner": "prauto-tester", "token": TOKEN}
    assert proofs[0]["path"] == (
        f"/api/v1/namespaces/{DS_NS}/services/dev-lock:8080/proxy/lock/renew"
    )
    assert sb.lock_paths("/lock/release") == [], (
        "the lock lived in the deleted cluster; not released"
    )
    assert not sb.token_file.exists(), "local lock token state must be discarded"
    acquire_idx = sb.index_of(
        lambda c: c["tool"] == "curl-detail" and c["url"].endswith("/acquire")
    )[0]
    last_helm_idx = max(sb.index_of(lambda c: c["tool"] == "helm"))
    assert acquire_idx < uninstall_idx
    assert acquire_idx < last_helm_idx, "final checks must run with the lock already held"
    assert sb.tool_calls("health-check") == []


def test_every_cluster_probe_is_pinned_to_the_env_files_context(sb: Sandbox) -> None:
    """spec: [reaped] condition 3 — "The cluster named by the env file's `DATASPOKE_KUBE_CLUSTER`
    context answers"; ambient current-context must never decide what is probed.
    """
    sb.reap()
    _assert_reap_returned(sb)
    for call in sb.tool_calls("kubectl"):
        assert call["argv"][:2] == ["--context", CLUSTER], call
    for call in sb.tool_calls("helm"):
        assert call["argv"][:2] == ["--kube-context", CLUSTER], call
    assert sb.tool_calls("kubectl") and sb.tool_calls("helm")


def test_helm_idleness_is_the_newest_release_across_all_dev_namespaces(sb: Sandbox) -> None:
    """spec: condition 5 — "the newest Helm release updated timestamp across the dev
    namespaces is older than the threshold". One recent release anywhere vetoes."""
    sb.helm_age(DATAHUB_NS, 5 * DAY)
    sb.helm_age(LANGFUSE_NS, 4 * DAY)
    sb.helm_age(DS_NS, 5 * DAY)
    sb.helm_age(DUMMY_NS, THRESHOLD - 120)  # the only fresh one, in the last namespace
    sb.reap()
    _assert_not_reaped(sb)
    assert len(sb.tool_calls("helm")) >= 4, (
        "every dev namespace must have been listed\n" + sb.output
    )


def test_default_threshold_is_7200_seconds_when_the_key_is_unset(sb: Sandbox) -> None:
    """spec: condition 1 — "default 7200"; and the shipped config.env carries that default."""
    config = (PRAUTO / "config.env").read_text().splitlines()
    assert "PRAUTO_DEV_ENV_REAP_IDLE_SECS=7200" in config

    sb.env["PRAUTO_DEV_ENV_REAP_IDLE_SECS"] = None
    sb.helm_age(DATAHUB_NS, 7100)
    sb.helm_age(DS_NS, 7100)
    sb.reap()
    _assert_not_reaped(sb)

    sb.helm_age(DATAHUB_NS, 7300)
    sb.helm_age(DS_NS, 7300)
    sb.reap()
    _assert_reap_returned(sb)
    assert len(sb.tool_calls("uninstall")) == 1, sb.output


def test_an_expired_keep_pin_does_not_block_reaping(sb: Sandbox) -> None:
    """spec: condition 6 — only a pin "whose time is in the future blocks reaping"."""
    sb.annotate_ds(_iso(sb.now - 3600))
    sb.reap()
    _assert_reap_returned(sb)
    assert len(sb.tool_calls("uninstall")) == 1, sb.output


# ======================================================================================
# Never reaps
# ======================================================================================


def _busy_helm(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.helm_age(DS_NS, THRESHOLD - 60)

    def backstop(s: Sandbox) -> None:
        assert s.tool_calls("helm"), "idleness was never measured"

    return backstop


def _pin_future(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.annotate_ds(_iso(sb.now + 3600))
    return lambda s: _assert_namespace_read(s)


def _pin_unparseable(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.annotate_ds("tomorrow-ish")
    return lambda s: _assert_namespace_read(s)


def _pin_empty(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.annotate_ds("")
    return lambda s: _assert_namespace_read(s)


def _assert_namespace_read(sb: Sandbox) -> None:
    # The keep-pin read asks for the DataSpoke namespace ALONE, as JSON — unlike the
    # first existence probe, which lists all four names.
    assert any(
        c["argv"][2:6] == ["get", "namespace", DS_NS, "--ignore-not-found"] and "json" in c["argv"]
        for c in sb.tool_calls("kubectl")
    ), "the DataSpoke namespace (keep pin) was never read\n" + sb.output


def _no_releases(sb: Sandbox) -> Callable[[Sandbox], None]:
    for ns in ALL_NS:
        sb.world["helm"][ns] = []
    return lambda s: _assert_namespace_read(s)


def _kubectl_fails(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.fault("kubectl.get_namespace")
    return lambda s: _assert_kubectl_was_asked(s)


def _kubectl_fails_only_under_lock(sb: Sandbox) -> Callable[[Sandbox], None]:
    # get_namespace call 1 = which namespaces exist; 2 = advisory keep-pin read;
    # 3 = the authoritative pin read, taken while the lock is held.
    sb.fault("kubectl.get_namespace", calls=[3])

    def backstop(s: Sandbox) -> None:
        assert s.lock_paths("/lock/acquire"), "the lock was never taken\n" + s.output
        assert len(s.lock_paths("/lock/release")) == 1, (
            "a lock taken for a reap that then failed closed must be given back\n" + s.output
        )

    return backstop


def _kubectl_hangs(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.fault("kubectl.get_namespace", mode="hang")
    sb.env["PRAUTO_DEV_ENV_NS_PROBE_TIMEOUT_SECS"] = "2"
    return lambda s: _assert_kubectl_was_asked(s)


def _assert_kubectl_was_asked(sb: Sandbox) -> None:
    assert sb.tool_calls("kubectl"), "kubectl was never invoked\n" + sb.output


def _helm_fails(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.fault("helm.list")
    return lambda s: _assert_helm_asked(s)


def _helm_fails_only_under_lock(sb: Sandbox) -> Callable[[Sandbox], None]:
    # Four namespaces: calls 1-4 are the advisory pass, 5 is the first under-lock listing.
    sb.fault("helm.list", calls=[5])

    def backstop(s: Sandbox) -> None:
        assert len(s.tool_calls("helm")) >= 5
        assert len(s.lock_paths("/lock/release")) == 1, s.output

    return backstop


def _helm_hangs(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.fault("helm.list", mode="hang")
    sb.env["PRAUTO_DEV_ENV_NS_PROBE_TIMEOUT_SECS"] = "2"
    return lambda s: _assert_helm_asked(s)


def _assert_helm_asked(sb: Sandbox) -> None:
    assert sb.tool_calls("helm"), "helm was never invoked\n" + sb.output


def _wrong_kube_context(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.world["cluster"] = "some-other-cluster"
    return lambda s: _assert_kubectl_was_asked(s)


def _lock_held(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.world["lock"]["acquire"] = 409

    def backstop(s: Sandbox) -> None:
        assert len(s.lock_paths("/lock/acquire")) == 1, s.output
        assert s.lock_paths("/lock/release") == [], (
            "we hold nothing after a 409; releasing would be reclaiming someone's lock\n" + s.output
        )

    return backstop


def _lock_transport_error(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.world["lock"]["acquire"] = "fail"
    return lambda s: _assert_acquire_tried(s)


def _lock_service_unreachable(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.world["lock"]["health"] = False

    def backstop(s: Sandbox) -> None:
        assert s.lock_paths("/health"), "the lock service was never probed\n" + s.output
        assert s.lock_paths("/lock/acquire") == [], (
            "an unreachable lock service is not evidence that nobody holds the lock\n" + s.output
        )

    return backstop


def _assert_acquire_tried(sb: Sandbox) -> None:
    assert sb.lock_paths("/lock/acquire"), "the lock acquire was never attempted\n" + sb.output


def _proof_rejected(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.world["proxy"] = "fail"
    return lambda s: _assert_proof_rejected(s)


def _proof_names_another_owner(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.world["proxy"] = "other_owner"
    return lambda s: _assert_proof_rejected(s)


def _proof_unreadable(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.world["proxy"] = "garbage"
    return lambda s: _assert_proof_rejected(s)


def _assert_proof_rejected(sb: Sandbox) -> None:
    assert sb.lock_paths("/lock/acquire"), sb.output
    assert len(sb.tool_calls("kubectl-body")) == 1, (
        "the token proof was never presented\n" + sb.output
    )
    assert len(sb.lock_paths("/lock/release")) == 1, (
        "a lock that is not the target cluster's must be released\n" + sb.output
    )


def _partial_cluster_without_dataspoke_ns(sb: Sandbox) -> Callable[[Sandbox], None]:
    del sb.world["namespaces"][DS_NS]
    return lambda s: _assert_kubectl_was_asked(s)


def _namespaces_all_absent(sb: Sandbox) -> Callable[[Sandbox], None]:
    sb.world["namespaces"] = {}
    return lambda s: _assert_kubectl_was_asked(s)


NEVER_REAP_CASES: dict[str, Callable[[Sandbox], Callable[[Sandbox], None]]] = {
    "newest-helm-update-inside-window": _busy_helm,
    "future-keep-until-pin": _pin_future,
    "unparseable-keep-until-pin": _pin_unparseable,
    "empty-keep-until-pin": _pin_empty,
    "no-helm-releases-to-measure": _no_releases,
    "kubectl-fails": _kubectl_fails,
    "kubectl-fails-on-the-final-check-under-lock": _kubectl_fails_only_under_lock,
    "kubectl-times-out": _kubectl_hangs,
    "helm-fails": _helm_fails,
    "helm-fails-on-the-final-check-under-lock": _helm_fails_only_under_lock,
    "helm-times-out": _helm_hangs,
    "kube-context-does-not-answer": _wrong_kube_context,
    "lock-held-409": _lock_held,
    "lock-acquire-transport-error": _lock_transport_error,
    "lock-service-unreachable": _lock_service_unreachable,
    "token-proof-rejected": _proof_rejected,
    "token-proof-names-another-owner": _proof_names_another_owner,
    "token-proof-unreadable": _proof_unreadable,
    "partial-cluster-without-dataspoke-namespace": _partial_cluster_without_dataspoke_ns,
    "no-dev-namespaces-at-all": _namespaces_all_absent,
}


@pytest.mark.parametrize("case", sorted(NEVER_REAP_CASES))
def test_a_single_failed_condition_means_do_nothing(sb: Sandbox, case: str) -> None:
    """spec: [reaped] — "fails closed: any probe that fails, times out, or is ambiguous means do
    nothing" and conditions 3-6 individually; [gates] (DataSpoke-namespace anchor: "a partial
    cluster without it is warned about and skipped on a fresh reap"; token proof by renewing
    through the service proxy). Every case departs from the reapable control world
    (test_reaps_an_ownerless_idle_cluster_...) by exactly one fact, and a per-case backstop
    proves the run reached the check that vetoed it.

    impl-defined (fail-closed), not stated in the spec: `no-helm-releases-to-measure` (spec
    only says "no releases left" counts as idle during recovery; a fresh reap needs a measured
    update), and the lock releases asserted by the `*-under-lock` and `token-proof-*` backstops
    (giving back a lock we took, rather than leaving it to expire).
    """
    backstop = NEVER_REAP_CASES[case](sb)
    sb.reap()
    _assert_not_reaped(sb)
    backstop(sb)


@pytest.mark.parametrize("value", ["0", "00", "", "abc", "-5", "1.5", "7200s", " "])
def test_threshold_zero_or_invalid_never_reaps_and_contacts_nothing(
    sb: Sandbox, value: str
) -> None:
    """spec: [reaped] condition 1 — `PRAUTO_DEV_ENV_REAP_IDLE_SECS` must be "greater than 0"
    (`0` disables) — covers "0" and "00". impl-defined (fail-closed): the empty / non-numeric /
    signed / fractional values, and "no contact at all for any of them" — the spec does not
    define them, but a typo or an empty override of a destructive cost knob must not arm it.
    """
    sb.env["PRAUTO_DEV_ENV_REAP_IDLE_SECS"] = value
    sb.reap()
    _assert_not_reaped(sb)
    assert sb.log() == [], (
        "a disabled reaper must not touch kubectl, helm or the lock\n" + sb.output
    )


def test_a_marker_sends_the_cluster_to_recovery_not_to_a_fresh_reap(sb: Sandbox) -> None:
    """spec: condition 2 — "No prauto marker exists (a marked cluster belongs to marker
    recovery)". The marker must be left exactly as found, and recovery must act on it."""
    sb.write_marker("provision")
    before = sb.marker.read_text()
    sb.reap()
    _assert_reap_returned(sb)
    assert sb.tool_calls("uninstall") == [], sb.output
    assert sb.log() == [], "the reaper must not even probe a marked cluster\n" + sb.output
    assert sb.marker.read_text() == before

    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    uninstalls = sb.tool_calls("uninstall")
    assert len(uninstalls) == 1, "recovery must finish the marked teardown\n" + sb.output
    assert uninstalls[0]["argv"] == _expected_uninstall_argv(sb)


@pytest.mark.parametrize("flag", ["DEV_ENV_TOUCHED_THIS_WAKE", "DEV_ENV_PROVISIONED"])
def test_a_wake_that_used_the_cluster_does_not_reap_it(sb: Sandbox, flag: str) -> None:
    """spec: [reaped] condition 2 — "this wake did not touch the cluster at all" (provisioning is
    the strongest touch; its marker/teardown belong to the provisioned-cluster path).
    """
    sb.run(f"{flag}=true\nreap_ownerless_idle_dev_env\necho REAP_RETURNED")
    _assert_not_reaped(sb)
    assert sb.log() == [], sb.output


def test_every_cluster_touching_entry_point_marks_the_wake_as_used() -> None:
    """spec: [reaped] condition 2 — "no health check, lock acquisition, deploy, or cluster test
    stage". Structural complement to the behavioural test below: each entry point must set
    DEV_ENV_TOUCHED_THIS_WAKE. Deploys and provisioning are checked only structurally here,
    because running them needs a real build/install.
    """
    source = (PRAUTO / "lib/dev-env.sh").read_text()

    def body(name: str) -> str:
        start = source.index(f"\n{name}() {{")
        end = source.index("\n}\n", start)
        return source[start:end]

    for fn in (
        "run_health_check",
        "dev_env_healthy",
        "dev_env_probe_healthy",
        "provision_dev_env",
        "acquire_required_dev_lock",
        "deploy_branch_api",
        "deploy_branch_frontend",
    ):
        assert "DEV_ENV_TOUCHED_THIS_WAKE=true" in body(fn), (
            f"{fn} does not mark the cluster as used"
        )


TOUCH_SCRIPTS = {
    "health-check": 'run_health_check "$REPO_DIR/helm-charts/bin/health-check.sh" "$DEV_ENV_FILE"',
    "lock-acquire": 'acquire_required_dev_lock 1 "full regression"\nrelease_required_dev_lock',
    "no-touch-control": ":",
}


@pytest.mark.parametrize("touch", sorted(TOUCH_SCRIPTS))
def test_a_real_cluster_touch_in_this_wake_stops_the_reaper_from_contacting_anything(
    sb: Sandbox, touch: str
) -> None:
    """spec: [reaped] condition 2 — "this wake did not touch the cluster at all: no health
    check, lock acquisition, deploy, or cluster test stage".

    Behavioural: run the real `run_health_check` / `acquire_required_dev_lock` against the
    fakes, then the reaper in the same shell, and assert the reaper adds no calls. Lock
    acquisition stands in for "cluster test stage": every cluster test stage (integration,
    api-wired, E2E) takes the cluster through `acquire_required_dev_lock` (or the equivalent
    inline acquires, which test_every_cluster_touching_entry_point_marks_the_wake_as_used
    covers structurally), after `dev_env_healthy`; a
    whole stage cannot be run against a fake cluster in a unit test. The no-touch control
    runs the identical script shape and DOES reap, so the silence is not an artefact.
    """
    script = "\n".join(
        [
            'regression_blocked() { echo "BLOCKED: $2"; }',
            "resolve_dev_env",
            TOUCH_SCRIPTS[touch],
            """printf '{"tool":"SENTINEL"}\\n' >> "$FAKE_CALLS" """,
            "reap_ownerless_idle_dev_env",
            "echo REAP_RETURNED",
        ]
    )
    sb.run(script)
    _assert_reap_returned(sb)
    assert "BLOCKED" not in sb.last.stdout, "the touching step itself failed\n" + sb.output
    log = sb.log()
    cut = next(i for i, c in enumerate(log) if c["tool"] == "SENTINEL")
    before, after = log[:cut], log[cut + 1 :]
    if touch == "no-touch-control":
        assert [c for c in after if c["tool"] == "uninstall"], (
            "control: an untouched wake must reap\n" + sb.output
        )
        return
    expected = "health-check" if touch == "health-check" else "curl-detail"
    assert any(c["tool"] == expected for c in before), (
        f"the {touch} step never reached the cluster/lock\n" + sb.output
    )
    assert after == [], "the reaper contacted the cluster after a touch\n" + sb.output
    assert not sb.marker.exists()


# -- the advisory pass: busy and pinned clusters never reach the lock service ------------


@pytest.mark.parametrize(
    "case",
    [
        "newest-helm-update-inside-window",
        "future-keep-until-pin",
        "unparseable-keep-until-pin",
        "no-helm-releases-to-measure",
        "kubectl-fails",
        "helm-fails",
        "kubectl-times-out",
        "partial-cluster-without-dataspoke-namespace",
        "no-dev-namespaces-at-all",
        "kube-context-does-not-answer",
    ],
)
def test_skip_paths_decided_before_the_lock_make_no_lock_service_contact(
    sb: Sandbox, case: str
) -> None:
    """spec: [reaped] conditions 4-6 + [re-gated] Gates bullet ("a lock-free advisory pass"): an
    ordinary busy or pinned cluster must not cost a lock acquisition, so a human's lock is never
    briefly taken.
    """
    backstop = NEVER_REAP_CASES[case](sb)
    sb.reap()
    _assert_not_reaped(sb)
    backstop(sb)
    assert sb.lock_requests() == [], (
        "the lock service was contacted although the cluster was not reapable\n" + sb.output
    )
    assert sb.token_file.exists() is False


# -- file-level gates: a prod (or incomplete) env file is never deleted by ---------------


def test_a_prod_named_env_file_is_never_reaped_and_nothing_is_contacted(sb: Sandbox) -> None:
    """spec: [gates] — "a non-prod basename"."""
    sb.write_env(_env_text(), rel="helm-charts/.env.prod")
    sb.reap()
    _assert_not_reaped(sb)
    assert sb.log() == [], sb.output


def test_prod_named_env_file_outside_the_helm_dir_is_still_refused(sb: Sandbox) -> None:
    """spec: [gates] — "a non-prod basename" (the basename, not the directory, decides)."""
    sb.write_env(_env_text(), rel="config/.env.prod")
    sb.reap()
    _assert_not_reaped(sb)
    assert sb.log() == [], sb.output


def test_an_env_file_with_prod_keys_and_no_dev_keys_is_never_reaped(sb: Sandbox) -> None:
    """spec: [gates] — "`DATASPOKE_DEV_*` keys present and no `DATASPOKE_PROD_*`"."""
    prod_text = "\n".join(
        [
            f"DATASPOKE_KUBE_CLUSTER={CLUSTER}",
            f"DATASPOKE_KUBE_DATASPOKE_NAMESPACE={DS_NS}",
            "DATASPOKE_PROD_KUBE_DATAHUB_NAMESPACE=datahub",
            "DATASPOKE_PROD_KUBE_LANGFUSE_NAMESPACE=langfuse",
            f"DATASPOKE_PROD_LOCK_URL={LOCK_URL}",
        ]
    )
    sb.write_env(prod_text + "\n")  # still named .env.dev: only its content says prod
    sb.reap()
    _assert_not_reaped(sb)
    assert sb.log() == [], sb.output


def test_an_env_file_mixing_prod_and_dev_keys_is_never_reaped(sb: Sandbox) -> None:
    """spec: [gates] — "`DATASPOKE_DEV_*` keys present and no `DATASPOKE_PROD_*`" (a file carrying
    both has PROD keys, so it fails the gate).
    """
    sb.write_env(_env_text(extra=("DATASPOKE_PROD_KUBE_DATAHUB_NAMESPACE=datahub",)))
    sb.reap()
    _assert_not_reaped(sb)
    assert sb.log() == [], sb.output


def test_a_missing_dev_lock_url_is_never_defaulted_to_localhost(sb: Sandbox) -> None:
    """spec: [gates] — "an explicitly set `DATASPOKE_DEV_LOCK_URL`"; [reaped] condition 4. With no
    known lock service there is no reap and no lock contact (no localhost fallback).
    """
    sb.write_env(_env_text(lock_url=None))
    sb.reap()
    _assert_not_reaped(sb)
    assert sb.log() == [], sb.output


@pytest.mark.parametrize(
    "drop",
    [
        "DATASPOKE_KUBE_CLUSTER",
        "DATASPOKE_KUBE_DATASPOKE_NAMESPACE",
        "DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE",
        "DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE",
        "DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE",
    ],
)
def test_an_env_file_that_does_not_name_the_cluster_and_all_namespaces_is_not_reaped(
    sb: Sandbox, drop: str
) -> None:
    """spec: [gates] — "all four dev namespace keys"; [reaped] condition 3 names the cluster by
    the env file's `DATASPOKE_KUBE_CLUSTER`.
    """
    text = "".join(
        line + "\n" for line in _env_text().splitlines() if not line.startswith(drop + "=")
    )
    sb.write_env(text)
    sb.reap()
    _assert_not_reaped(sb)
    assert sb.log() == [], sb.output


def test_a_namespace_value_that_could_become_a_kubectl_option_is_refused(sb: Sandbox) -> None:
    """impl-defined (fail-closed): the spec does not name namespace-value validation. Reason: an
    option-shaped value (`--selector=...`) makes kubectl answer "nothing found", which would
    read as "namespaces gone".
    """
    sb.write_env(_env_text().replace(f"={DATAHUB_NS}", "=--selector=a=b"))
    sb.reap()
    _assert_not_reaped(sb)
    assert sb.log() == [], sb.output


def test_an_env_file_whose_sourced_values_differ_from_its_literal_lines_is_refused(
    sb: Sandbox,
) -> None:
    """spec: [gates] — "values that read the same when the file is sourced the way `uninstall.sh`
    sources it".
    """
    sb.write_env(_env_text(extra=('DATASPOKE_KUBE_CLUSTER="$OTHER_CLUSTER"',)))
    sb.reap()
    _assert_not_reaped(sb)
    assert sb.log() == [], sb.output


# ======================================================================================
# Failed / unconfirmed uninstall
# ======================================================================================


def test_a_failed_uninstall_keeps_the_reap_marker_and_prints_none_of_its_output(
    sb: Sandbox,
) -> None:
    """spec: [reaped] — "Uninstall output goes to a private local teardown log, never the
    scheduler log; only a fixed line is logged"; "Teardown verifies deletion before clearing
    its marker" (a failed teardown leaves the marker for a retry).
    """
    sb.world["uninstall"] = {"exit": 3, "deletes": False}
    sb.reap()
    _assert_reap_returned(sb)
    assert len(sb.tool_calls("uninstall")) == 1, sb.output
    assert sb.marker.exists(), "a failed uninstall must leave the marker for a retry\n" + sb.output
    assert sb.marker_json()["kind"] == "reap"
    assert SECRET not in sb.combined
    logs = sorted((sb.prauto / "state").glob("teardown-*"))
    assert len(logs) == 1, "uninstall output belongs in a private teardown log"
    assert SECRET in logs[0].read_text()
    assert stat.S_IMODE(logs[0].stat().st_mode) == 0o600


def test_a_zero_exit_that_deleted_nothing_does_not_clear_the_marker(sb: Sandbox) -> None:
    """spec: "Teardown verifies deletion before clearing its marker" — "A zero exit from
    uninstall.sh is not itself proof of deletion".
    """
    sb.world["uninstall"] = {"exit": 0, "deletes": False}
    sb.reap()
    _assert_reap_returned(sb)
    assert len(sb.tool_calls("uninstall")) == 1
    assert sb.marker.exists(), sb.output
    assert sb.marker_json()["kind"] == "reap"


def test_an_unanswerable_post_uninstall_probe_does_not_clear_the_marker(sb: Sandbox) -> None:
    """spec: "Teardown verifies deletion before clearing its marker" — the marker is cleared only
    once the executor has confirmed the namespaces are gone; an API error is "could not ask".
    """
    sb.world["uninstall"] = {
        "exit": 0,
        "deletes": True,
        "set_faults": {"kubectl.get_namespace": {"mode": "fail", "calls": "all"}},
    }
    sb.reap()
    _assert_reap_returned(sb)
    assert len(sb.tool_calls("uninstall")) == 1
    assert sb.marker.exists(), (
        "deletion was never confirmed, yet the marker was cleared\n" + sb.output
    )


def test_a_marker_that_cannot_be_written_means_no_uninstall(sb: Sandbox) -> None:
    """spec: [reaped] — the marker is written "Before the uninstall"; fail-closed: no persisted
    marker, no uninstall. impl-defined (fail-closed): releasing the lock taken for the reap —
    the spec is silent, but stranding an acquired lock would block a human.
    """
    sb.block_marker_writes()
    sb.reap()
    _assert_reap_returned(sb)
    assert sb.lock_paths("/lock/acquire"), (
        "the reap never got as far as writing its marker\n" + sb.output
    )
    assert not sb.marker.exists()
    assert sb.tool_calls("uninstall") == [], sb.output
    assert len(sb.lock_paths("/lock/release")) == 1, (
        "the lock taken for the reap must be given back"
    )


# ======================================================================================
# Reap-marker recovery
# ======================================================================================


def test_recovery_retries_a_reap_marker_when_the_cluster_is_unchanged(sb: Sandbox) -> None:
    """spec: [re-gated] — Gates bullet (advisory pass, token-bound lock held through the uninstall,
    re-check under the lock; idleness measured against the marker time) and Already-gone
    bullet (marker cleared once the namespaces are confirmed absent); [reaped] — "a reap
    interrupted mid-uninstall is re-evaluated by a later heartbeat's marker recovery".

    The "acquire, then no release" assertion on this retry path traces to [reaped]:
    "after confirmed deletion the reaper discards its local lock token state rather than
    releasing — a release against a deleted service is not an error to retry"; the token file
    is asserted discarded for the same reason.
    """
    sb.write_marker("reap")
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    uninstalls = sb.tool_calls("uninstall")
    assert len(uninstalls) == 1, sb.output
    assert uninstalls[0]["argv"] == _expected_uninstall_argv(sb)
    assert not sb.marker.exists(), (
        "deleted and confirmed, so the marker must be cleared\n" + sb.output
    )
    # The dev-lock service still existed, so the retry still went through the lock...
    assert len(sb.lock_paths("/lock/acquire")) == 1
    # ...and, deletion being confirmed, the claim is discarded rather than released.
    assert sb.lock_paths("/lock/release") == []
    assert not sb.token_file.exists()


def test_recovery_drops_a_reap_marker_when_helm_was_updated_after_the_marker(sb: Sandbox) -> None:
    """spec: [re-gated] — "Conclusive change: drop the marker, delete nothing. A newer Helm
    update" and the Gates bullet ("no Helm release updated since the marker time"). The lock
    service is not contacted because the Gates bullet runs a lock-free advisory pass first.
    """
    sb.write_marker("reap", age=1000)
    sb.helm_age(DATAHUB_NS, 200)  # a human reinstall, after the reap began
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == [], sb.output
    assert not sb.marker.exists(), "a dropped marker must not be retried forever\n" + sb.output
    assert sb.tool_calls("helm"), "recovery never measured Helm activity"
    assert sb.lock_requests() == [], "a conclusive 'changed' verdict needs no lock"


def test_recovery_drops_a_reap_marker_when_helm_was_updated_after_it_even_without_dev_lock(
    sb: Sandbox,
) -> None:
    """spec: [re-gated] — Lockless retry bullet ("no Helm update since the marker") combined with
    "Conclusive change ... A newer Helm update": a lockless-eligible cluster is still not
    deleted when it was reinstalled after the marker.
    """
    sb.write_marker("reap", age=1000)
    sb.world["dev_lock_service"] = False
    sb.helm_age(DS_NS, 200)
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == [], sb.output
    assert not sb.marker.exists()
    assert sb.lock_requests() == []


def test_recovery_drops_a_reap_marker_when_a_namespace_was_recreated_after_it(
    sb: Sandbox,
) -> None:
    """spec: [re-gated] — Lockless retry bullet: "no dev namespace created after the marker (a
    creation drops the marker, like any conclusive change)". A reinstall creates namespaces
    before any release exists, and dummy-data has no release, so creation time is the earlier
    trace.
    """
    sb.write_marker("reap", age=1000)
    sb.world["dev_lock_service"] = False  # the lockless retry, where this check lives
    sb.world["namespaces"][DUMMY_NS]["created"] = _iso(sb.now - 300)
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == [], sb.output
    assert not sb.marker.exists()
    assert sb.lock_requests() == []


def test_recovery_drops_a_reap_marker_when_a_keep_pin_is_now_in_effect(sb: Sandbox) -> None:
    """spec: [re-gated] — "Conclusive change: ... a future keep pin"; [reaped] condition 6
    (the pin is the human escape hatch).
    """
    sb.write_marker("reap")
    sb.annotate_ds(_iso(sb.now + 3600))
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == [], sb.output
    assert not sb.marker.exists()
    assert sb.lock_requests() == []


def test_recovery_keeps_the_reap_marker_when_the_lock_is_held(sb: Sandbox) -> None:
    """spec: [re-gated] — "Undetermined: keep the marker for a later wake. ... a lock conflict";
    [reaped] condition 4 ("any acquire conflict or failure means do nothing"). No release is
    asserted because a 409 means we hold nothing.
    """
    sb.write_marker("reap")
    before = sb.marker.read_text()
    sb.world["lock"]["acquire"] = 409
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == [], sb.output
    assert sb.marker.read_text() == before
    assert sb.lock_paths("/lock/release") == []


@pytest.mark.parametrize(
    "fault",
    [
        ("kubectl.get_namespace", "fail"),
        ("helm.list", "fail"),
        ("kubectl.get_service", "fail"),
        ("kubectl.get_namespace", "hang"),
    ],
    ids=[
        "namespace-probe-error",
        "helm-error",
        "dev-lock-service-probe-error",
        "namespace-probe-timeout",
    ],
)
def test_recovery_keeps_the_reap_marker_when_a_probe_cannot_answer(
    sb: Sandbox, fault: tuple[str, str]
) -> None:
    """spec: [re-gated] — "Undetermined: keep the marker for a later wake. A probe error or
    timeout"; [reaped] fail-closed rule.
    """
    sb.write_marker("reap")
    before = sb.marker.read_text()
    op, mode = fault
    sb.fault(op, mode=mode)
    if mode == "hang":
        sb.env["PRAUTO_DEV_ENV_NS_PROBE_TIMEOUT_SECS"] = "2"
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == [], sb.output
    assert sb.marker.read_text() == before, (
        "an undetermined probe must keep the marker\n" + sb.output
    )


def test_recovery_keeps_the_reap_marker_when_the_lock_service_is_unreachable(sb: Sandbox) -> None:
    """spec: [re-gated] — "Undetermined: ... an unreachable lock service"; [reaped]
    condition 4 ("An unreachable lock service is not evidence that no holder exists").
    """
    sb.write_marker("reap")
    sb.world["lock"]["health"] = False
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == []
    assert sb.marker.exists()
    assert sb.lock_paths("/lock/acquire") == []


def test_recovery_keeps_the_marker_and_releases_when_the_token_proof_is_rejected(
    sb: Sandbox,
) -> None:
    """spec: [re-gated] — "Undetermined: ... a rejected token proof"; [gates] (the lock is proved
    to belong to the target cluster by renewing the token through the service proxy).
    impl-defined (fail-closed): the lock release asserted here — the spec is silent on it, but
    a lock that is not the target cluster's must not be left held.
    """
    sb.write_marker("reap")
    sb.world["proxy"] = "fail"
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == []
    assert sb.marker.exists()
    assert len(sb.lock_paths("/lock/release")) == 1


def test_recovery_retries_without_the_lock_when_the_dev_lock_service_is_already_gone(
    sb: Sandbox,
) -> None:
    """spec: [re-gated] — Lockless retry bullet: "a clean API-server answer that the dev-lock
    Service ... is absent makes the retry run without the lock, after the lock-free checks
    pass". The lock URL is therefore never contacted.
    """
    sb.write_marker("reap")
    sb.world["dev_lock_service"] = False
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    uninstalls = sb.tool_calls("uninstall")
    assert len(uninstalls) == 1, sb.output
    assert uninstalls[0]["argv"] == _expected_uninstall_argv(sb)
    assert sb.lock_requests() == [], "no lock contact is possible or needed\n" + sb.output
    assert not sb.marker.exists()


def test_recovery_retries_without_the_lock_when_the_dataspoke_namespace_is_already_gone(
    sb: Sandbox,
) -> None:
    """spec: [re-gated] — Lockless retry bullet ("... or the DataSpoke namespace is absent"); with
    the namespace gone no keep pin can exist, so only the Helm / creation checks apply.
    """
    sb.write_marker("reap")
    del sb.world["namespaces"][DS_NS]
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert len(sb.tool_calls("uninstall")) == 1, sb.output
    assert sb.lock_requests() == []
    assert not sb.marker.exists()


def test_recovery_with_a_failed_retry_keeps_the_marker_as_a_reap_marker(sb: Sandbox) -> None:
    """spec: [re-gated] — a reap marker "records only an idleness verdict ... so recovery runs the
    reap gates again": a failed retry must leave the marker a `reap` marker (never rewritten as
    `provision`, which would be retried unconditionally); "Teardown verifies deletion before
    clearing its marker"; [reaped] (output goes to the private teardown log).
    """
    sb.write_marker("reap")
    sb.world["uninstall"] = {"exit": 1, "deletes": False}
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert len(sb.tool_calls("uninstall")) == 1
    assert sb.marker.exists()
    assert sb.marker_json()["kind"] == "reap", "a reap marker must never become a provision marker"
    assert SECRET not in sb.combined


def test_recovery_clears_a_reap_marker_whose_cluster_is_already_gone(sb: Sandbox) -> None:
    """spec: [re-gated] — "Already gone. If the namespaces are confirmed absent, the marker is
    cleared." Nothing is uninstalled and no lock is needed.
    """
    sb.write_marker("reap")
    sb.world["namespaces"] = {}
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == []
    assert not sb.marker.exists()
    assert sb.lock_requests() == []


def test_recovery_drops_a_reap_marker_whose_env_file_no_longer_passes_the_dev_gate(
    sb: Sandbox,
) -> None:
    """spec: [re-gated] — "Conclusive change: ... the env file no longer passing the gate"; the gate
    itself is [gates] (here: DATASPOKE_DEV_LOCK_URL no longer set explicitly). The gate is
    local, so nothing is contacted.
    """
    sb.write_marker("reap")
    sb.write_env(_env_text(lock_url=None))
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == []
    assert not sb.marker.exists()
    assert sb.log() == []


@pytest.mark.parametrize("stamp", ["not-a-time", "", "2026-13-45T99:99:99Z"])
def test_recovery_drops_a_reap_marker_with_no_readable_timestamp(sb: Sandbox, stamp: str) -> None:
    """spec: [re-gated] — "Conclusive change: ... an unreadable marker timestamp" (no start time,
    so no basis for "no release updated since the marker time").
    """
    sb.write_marker("reap", provisioned_at=stamp)
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == []
    assert not sb.marker.exists()


def _helm_listed_namespaces(sb: Sandbox) -> list[str]:
    return [c["argv"][c["argv"].index("-n") + 1] for c in sb.tool_calls("helm")]


def _four_namespace_json_reads(sb: Sandbox) -> list[int]:
    """Log indexes of the namespace-creation reads: one `get namespace <all four> -o json`."""
    return sb.index_of(
        lambda c: (
            c["tool"] == "kubectl"
            and c["argv"][2:4] == ["get", "namespace"]
            and "json" in c["argv"]
            and sum(a in ALL_NS for a in c["argv"]) == len(ALL_NS)
        )
    )


def test_lockless_retry_rechecks_helm_immediately_before_the_uninstall(sb: Sandbox) -> None:
    """spec: [re-gated] Lockless retry bullet — "after the lock-free checks pass twice, the
    second time immediately before the uninstall". Only the SECOND Helm pass sees the veto
    here (its first listing fails), so a retry that checked once would uninstall.

    Undetermined probe -> keep the marker ([re-gated] "Undetermined"), delete nothing."""
    sb.write_marker("reap")
    sb.world["dev_lock_service"] = False
    # Advisory pass = one listing per dev namespace (calls 1-4); the re-check starts at 5.
    sb.fault("helm.list", calls=[5])
    before = sb.marker.read_text()
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == [], sb.output
    assert sb.marker.read_text() == before, "an undetermined re-check must keep the marker"
    assert sb.lock_requests() == [], "the lockless path never contacts the lock service"
    # Backstop: the advisory pass really did list every namespace and pass, and the failing
    # call was the first listing of a second pass (not some earlier call).
    listed = _helm_listed_namespaces(sb)
    assert sorted(listed[:4]) == sorted(ALL_NS), listed
    assert len(listed) == 5, "the re-check never ran (or ran further than the vetoed call)"
    assert listed[4] == listed[0], "the second pass must start over from the first namespace"


def test_lockless_retry_rereads_namespace_creation_immediately_before_the_uninstall(
    sb: Sandbox,
) -> None:
    """spec: [re-gated] Lockless retry bullet — "no dev namespace created after the marker (a
    creation drops the marker, like any conclusive change)", with the checks run "twice, the
    second time immediately before the uninstall". Here a namespace is recreated AFTER the
    advisory pass, between the Helm re-check and the last probe; only the final namespace
    read can see it, so a retry that read creation times once would uninstall.

    Conclusive change -> drop the marker, delete nothing."""
    sb.write_marker("reap", age=1000)
    sb.world["dev_lock_service"] = False
    # get_namespace calls on this path: 1 existence probe, 2 keep-pin read (advisory),
    # 3 creation read (first), 4 keep-pin read (re-check), 5 creation read (immediately
    # before the uninstall). The recreation lands just before call 5 answers.
    sb.mutate_on_call("kubectl.get_namespace", 5, namespace_created={DUMMY_NS: _iso(sb.now - 300)})
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == [], sb.output
    assert not sb.marker.exists(), "a conclusive change must drop the marker\n" + sb.output
    assert sb.lock_requests() == []
    # Backstop: the first creation read (advisory side) found nothing and the Helm re-check
    # ran, so the veto really came from the second creation read.
    reads = _four_namespace_json_reads(sb)
    assert len(reads) == 2, ("expected a creation read on both sides of the Helm re-check", reads)
    helm_idx = sb.index_of(lambda c: c["tool"] == "helm")
    assert len(helm_idx) == 2 * len(ALL_NS), "the Helm advisory pass and re-check must both run"
    assert reads[0] < helm_idx[len(ALL_NS)] < reads[1], "the last read must follow the re-check"
    assert len(sb.tool_calls("kubectl-body")) == 0


def test_recovery_counts_no_releases_left_as_idle(sb: Sandbox) -> None:
    """spec: [re-gated] — Gates bullet: "'no releases left' counts as idle" (a partly-run
    uninstall removes the releases first), so the retry proceeds through the lock."""
    sb.write_marker("reap")
    for ns in ALL_NS:
        sb.world["helm"][ns] = []
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert len(sb.tool_calls("uninstall")) == 1, sb.output
    assert len(sb.lock_paths("/lock/acquire")) == 1
    assert not sb.marker.exists()


def test_a_recreated_namespace_is_not_a_signal_while_the_lock_can_still_be_taken(
    sb: Sandbox,
) -> None:
    """spec: [re-gated] — Lockless retry bullet: the creation check is "the extra conclusive
    signal used only here". With the dev-lock Service present the token-bound path applies,
    and a namespace created after the marker (no Helm update) does not by itself drop it."""
    sb.write_marker("reap", age=1000)
    sb.world["namespaces"][DUMMY_NS]["created"] = _iso(sb.now - 300)
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert len(sb.tool_calls("uninstall")) == 1, sb.output
    assert len(sb.lock_paths("/lock/acquire")) == 1


@pytest.mark.parametrize("kind", [None, "provision"], ids=["legacy-no-kind", "explicit-provision"])
def test_a_provision_or_legacy_marker_keeps_the_unconditional_retry(
    sb: Sandbox, kind: str | None
) -> None:
    """spec: [re-gated] — "a marker with no kind reads as `provision`" and "A `provision` marker
    records a cluster prauto built, so its teardown is retried unconditionally": even a busy,
    pinned, lock-held cluster is deleted, with no idleness or lock check.
    """
    sb.write_marker(kind)
    sb.helm_age(DS_NS, 5)
    sb.annotate_ds(_iso(sb.now + 7 * DAY))
    sb.world["lock"]["acquire"] = 409
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    uninstalls = sb.tool_calls("uninstall")
    assert len(uninstalls) == 1, sb.output
    assert uninstalls[0]["argv"] == _expected_uninstall_argv(sb)
    assert sb.tool_calls("helm") == [], "a provision retry must not consult Helm idleness"
    assert sb.lock_requests() == [], "a provision retry must not contact the lock service"
    assert not sb.marker.exists(), "deleted and confirmed, so the marker is cleared"


def test_an_unknown_marker_kind_is_treated_as_a_reap_marker(sb: Sandbox) -> None:
    """spec: [re-gated] — "an unknown kind is handled as `reap`, the strict case": here a newer
    Helm update drops the marker instead of triggering an unconditional uninstall.
    """
    sb.write_marker("something-new")
    sb.helm_age(DS_NS, 200)
    sb.recover()
    _assert_reap_returned(sb, "RECOVER_RETURNED")
    assert sb.tool_calls("uninstall") == []
    assert not sb.marker.exists()


def test_write_dev_env_state_marker_records_the_kind_and_defaults_to_provision(sb: Sandbox) -> None:
    """spec: [re-gated] — "The durable marker records its origin, `provision` or `reap`"; [reaped]
    ("tagged as a reap"). impl-defined (fail-closed): the 0600 mode and the writer's default
    of `provision` — the spec only fixes how a *missing* kind is read; a private, non-reap
    default keeps provisioning's behaviour unchanged.
    """
    sb.run(
        f"write_dev_env_state_marker {shlex.quote(str(sb.env_file))}\n"
        'cp "$DEV_ENV_STATE_FILE" "$PRAUTO_DIR/default.json"\n'
        f"write_dev_env_state_marker {shlex.quote(str(sb.env_file))} reap\n"
        "echo DONE"
    )
    assert sb.last is not None and sb.last.returncode == 0, sb.output
    default = json.loads((sb.prauto / "default.json").read_text())
    assert default["kind"] == "provision"
    assert default["env_file"] == str(sb.env_file)
    assert sb.marker_json()["kind"] == "reap"
    assert stat.S_IMODE(sb.marker.stat().st_mode) == 0o600


# ======================================================================================
# Heartbeat gating (cleanup() run in isolation)
# ======================================================================================


def _cleanup_block() -> str:
    """The real cleanup()/handle_signal() definitions from heartbeat.sh: everything from the
    first state variable up to the trap installation. heartbeat.sh itself cannot be
    sourced: its top level acquires the heartbeat lock, loads config, calls gh and runs a
    whole wake."""
    source = (PRAUTO / "heartbeat.sh").read_text()
    start = re.search(r"^WORKTREE_DIR=", source, re.M)
    end = re.search(r"^trap cleanup EXIT", source, re.M)
    assert start and end and start.start() < end.start()
    block = source[start.start() : end.start()]
    assert re.search(r"^cleanup\(\) \{", block, re.M)
    assert re.search(r"^handle_signal\(\) \{", block, re.M)
    assert "acquire_lock" not in block
    return block


def _acquire_region() -> str:
    """The top-level heartbeat.sh region from the trap installation through the assignment
    that records ownership of the heartbeat lock (the acquire-or-exit step), minus library
    `source` lines. Extracted by structure (first `trap cleanup EXIT` .. first top-level
    `HEARTBEAT_LOCK_HELD=true`), so reordering or adding statements inside it is harmless."""
    source = (PRAUTO / "heartbeat.sh").read_text()
    start = re.search(r"^trap cleanup EXIT", source, re.M)
    flag = re.search(r"^HEARTBEAT_LOCK_HELD=true[ \t]*$", source, re.M)
    assert start and flag and start.start() < flag.start()
    lines = source[start.start() : flag.end()].splitlines()
    return "\n".join(ln for ln in lines if not ln.lstrip().startswith("source "))


def _run_cleanup(
    tmp_path: Path, tail: str, *, worker_id: str = "tester", strict: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    events = tmp_path / "events"
    script = "\n".join(
        [
            "set -euo pipefail" if strict else "set -uo pipefail",
            f"EVENTS={shlex.quote(str(events))}",
            f"REPO_DIR={shlex.quote(str(tmp_path))}",
            f"PRAUTO_WORKER_ID={shlex.quote(worker_id)}",
            "info() { :; }",
            "warn() { :; }",
            'release_required_dev_lock() { echo release_required_dev_lock >> "$EVENTS"; }',
            'dev_lock_stop_renewer() { echo stop_renewer >> "$EVENTS"; }',
            'teardown_provisioned_dev_env() { echo teardown >> "$EVENTS"; }',
            'reap_ownerless_idle_dev_env() { echo reap >> "$EVENTS"; }',
            'release_lock() { echo release_lock >> "$EVENTS"; }',
            _cleanup_block(),
            tail,
        ]
    )
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", script], capture_output=True, check=False, text=True, timeout=60
    )
    return result, events.read_text().split() if events.exists() else []


def test_cleanup_reaps_after_its_own_teardown(tmp_path: Path) -> None:
    """spec: [reaped] — "At the end of every heartbeat, after its own provisioned-cluster
    teardown, prauto therefore reaps"; [gates] — the reap runs when this heartbeat holds its
    heartbeat lock.

    impl-defined (fail-closed): the two ordering assertions below are not in the spec text.
    This wake's own dev-env lock is released first because the reaper takes and manages its
    own dev-env lock; the reap precedes the heartbeat-lock release so a second heartbeat
    cannot start and race the uninstall."""
    result, events = _run_cleanup(tmp_path, "HEARTBEAT_LOCK_HELD=true\ntrap cleanup EXIT\nexit 0")
    assert result.returncode == 0, result.stderr
    assert events.count("reap") == 1, events
    assert events.index("teardown") < events.index("reap"), events  # spec'd
    assert events.index("release_required_dev_lock") < events.index("reap"), events  # impl-defined
    assert events.index("reap") < events.index("release_lock"), events  # impl-defined


def test_cleanup_does_not_reap_when_this_process_never_held_the_heartbeat_lock(
    tmp_path: Path,
) -> None:
    """spec: [gates] — "The end-of-heartbeat reap runs only when this heartbeat actually holds
    its heartbeat lock". A wake that exited early because another heartbeat held the lock
    must not touch a cluster."""
    result, events = _run_cleanup(tmp_path, "trap cleanup EXIT\nexit 0")  # flag stays false
    assert result.returncode == 0, result.stderr
    assert "reap" not in events, events
    assert "release_lock" in events, "cleanup itself must still have run: " + str(events)
    assert "teardown" in events


@pytest.mark.parametrize(("signal", "status"), [("TERM", 143), ("INT", 130)])
def test_cleanup_does_not_reap_when_the_wake_is_being_killed(
    tmp_path: Path, signal: str, status: int
) -> None:
    """spec: [gates] — the reap runs only when "its cleanup was not signal-driven (SIGINT and
    SIGTERM exit fast)": a dying wake must not start a long, networked cluster deletion."""
    result, events = _run_cleanup(
        tmp_path,
        "\n".join(
            [
                "HEARTBEAT_LOCK_HELD=true",
                "trap cleanup EXIT",
                "trap 'handle_signal 130' INT",
                "trap 'handle_signal 143' TERM",
                f"kill -{signal} $$",
                "sleep 5",
                "echo NOT-REACHED",
            ]
        ),
    )
    assert result.returncode == status, (result.returncode, result.stderr)
    assert "NOT-REACHED" not in result.stdout
    assert "reap" not in events, events
    # The rest of the cleanup still ran, exactly once (handle_signal disarms EXIT).
    assert events.count("teardown") == 1 and events.count("release_lock") == 1, events


def test_cleanup_does_not_reap_without_a_worker_identity(tmp_path: Path) -> None:
    """impl-defined (fail-closed): the spec does not mention the worker identity. Reason: the
    reaper holds the dev-env lock as prauto-<worker id>, and with no loaded config there is
    no identity to hold it under."""
    result, events = _run_cleanup(
        tmp_path, "HEARTBEAT_LOCK_HELD=true\ntrap cleanup EXIT\nexit 0", worker_id=""
    )
    assert result.returncode == 0, result.stderr
    assert "reap" not in events, events
    assert "release_lock" in events


def test_a_failing_reaper_cannot_break_the_cleanup_exit_path(tmp_path: Path) -> None:
    """spec: [reaped] — reaping is "cost hygiene"; the end-of-heartbeat step must never
    prevent the heartbeat from releasing its own lock."""
    result, events = _run_cleanup(
        tmp_path,
        'reap_ownerless_idle_dev_env() { echo reap >> "$EVENTS"; return 7; }\n'
        "HEARTBEAT_LOCK_HELD=true\ntrap cleanup EXIT\nexit 0",
        strict=True,
    )
    assert result.returncode == 0, result.stderr
    assert events.count("reap") == 1
    assert "release_lock" in events, "a reaper failure must not skip the heartbeat-lock release"


@pytest.mark.parametrize(
    ("acquire_rc", "reaps"), [(0, 1), (1, 0)], ids=["acquired", "held-by-peer"]
)
def test_the_heartbeat_lock_flag_is_set_only_when_the_lock_was_actually_acquired(
    tmp_path: Path, acquire_rc: int, reaps: int
) -> None:
    """spec: [gates] — the reap runs only "when this heartbeat actually holds its heartbeat
    lock". Behavioural: the real acquire-or-exit region of heartbeat.sh is run with a stubbed
    `acquire_lock` and the real `cleanup`. A failed acquire exits early with the flag unset
    (no reap); a successful one sets it and the exit-time cleanup reaps."""
    result, events = _run_cleanup(
        tmp_path,
        "\n".join(
            [
                f"acquire_lock() {{ return {acquire_rc}; }}",
                _acquire_region(),
                'echo "FLAG=$HEARTBEAT_LOCK_HELD"',
                "exit 0",
            ]
        ),
        strict=True,
    )
    assert result.returncode == 0, result.stderr
    assert events.count("reap") == reaps, (events, result.stdout)
    if acquire_rc == 0:
        assert "FLAG=true" in result.stdout, "the success path never reached the flag"
    else:
        assert "FLAG=" not in result.stdout, "the failed-acquire path must exit before the flag"
    assert "release_lock" in events, "cleanup must run on both paths"

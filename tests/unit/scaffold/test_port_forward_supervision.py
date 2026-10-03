"""Hermetic tests for the supervised `helm-charts/bin/port-forward.sh` (#157).

Cluster-free and kubectl-free. The harnesses source the real `helm-charts/bin/lib/helpers.sh`,
slice the real supervision functions / spec loop / startup tail / log-dir block out of
`helm-charts/bin/port-forward.sh`, stub `info`/`warn`/`error` onto stdout and run under
`/bin/bash` (3.2 on macOS) so a bash-4-only construct fails here, not on an operator's laptop.

`kubectl` is a PATH stub (python3). `port-forward` binds a real listener on a free port chosen
by the test and prints kubectl's own `Forwarding from 127.0.0.1:<port> -> N` line, or kubectl's
real bind-failure text and exit 1 when the port is taken. A one-shot per-port control file picks
a misbehaviour (late readiness line, no readiness line, stale, per-connection chatter, idle).
The production script's ports are fixed (9201 and friends), so the tests drive the sliced
functions with test-supplied specs and never run the whole script: that would collide with a real
forward on a developer machine. Only `--help`, bad flags and static checks touch the real script.

Assertions derive from `spec/feature/HELM_CHART.md §Port-forward supervision` and the issue's
acceptance criteria (a squatted port is reported failed and not counted; a lost forward is
detected, logged and respawned; Ctrl-C leaves nothing behind), not from incidental behaviour.
Where a test pins something the spec leaves to the implementation, its docstring says so.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "helm-charts/bin/port-forward.sh"
HELPERS = ROOT / "helm-charts/bin/lib/helpers.sh"

# The macOS system bash (3.2) is the platform that rules out `wait -n` and associative arrays.
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"
HAVE_LSOF = subprocess.run(["which", "lsof"], capture_output=True, check=False).returncode == 0

# One forward-loop budget: generous so a loaded CI box does not flake, short enough to stay quick.
WAIT_SECS = 25.0

# Stub kubectl. `get svc/<name> -n <ns>` succeeds for names listed in PF_STUB_SERVICES.
# `port-forward ... <local>:<remote> ...` binds <local> (SO_REUSEADDR, as Go does) or fails the way
# kubectl does. Modes (one-shot, read from and removed at spawn): idle | delayline | noline |
# stale | chatter. Records its pid and one line per attempt so tests can see respawns.
_KUBECTL_STUB = """#!{python}
import os, re, signal, socket, sys, time

args = sys.argv[1:]
d = os.environ["PF_STUB_DIR"]
if args and args[0] == "get":
    ns = args[args.index("-n") + 1]
    name = args[1].split("/", 1)[1]
    sys.exit(0 if f"{{ns}}/{{name}}" in os.environ.get("PF_STUB_SERVICES", "").split(",") else 1)
if not args or args[0] != "port-forward":
    sys.exit(2)

lport, rport = next(a for a in args if re.fullmatch(r"\\d+:\\d+", a)).split(":")
signal.alarm(120)  # safety net: never outlive a crashed test run
with open(f"{{d}}/pid-{{lport}}", "w") as f:
    f.write(str(os.getpid()))
with open(f"{{d}}/attempts-{{lport}}", "a") as f:
    f.write("x\\n")
mode = ""
mode_file = f"{{d}}/mode-{{lport}}"
if os.path.exists(mode_file):
    mode = open(mode_file).read().strip()
    os.remove(mode_file)

if mode == "idle":  # alive, never binds, prints nothing
    time.sleep(120)
    sys.exit(0)

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(lport)))
except OSError:
    sys.stderr.write(
        f"Unable to listen on port {{lport}}: Listeners failed to create with the following "
        f"errors: [unable to create listener: Error listen tcp4 127.0.0.1:{{lport}}: bind: "
        f"address already in use]\\n"
        f"error: unable to listen on any of the requested ports: [{{{{{{lport}} {{rport}}}}}}]\\n"
    )
    sys.stderr.flush()
    sys.exit(1)
s.listen(16)

def say(text):
    print(text, flush=True)

if mode == "delayline":
    time.sleep(1.5)
if mode != "noline":
    say(f"Forwarding from 127.0.0.1:{{lport}} -> {{rport}}")

events = []
if mode == "stale":
    events.append((2.0, "E0101 00:00:00.000000 1 portforward.go:1] lost connection to pod"))
if mode == "chatter":
    events.append((1.0, f"Handling connection for {{lport}}"))
    events.append((1.0, f"E0101 00:00:00.000000 1 portforward.go:1] "
                        f"error forwarding port {{rport}} to pod abc, uid : exit status 1"))
    events.append((1.0, f"an error occurred forwarding {{lport}} -> {{rport}}: "
                        f"error forwarding port {{rport}} to pod abc"))
start = time.time()
s.settimeout(0.1)
while True:
    while events and time.time() - start >= events[0][0]:
        say(events.pop(0)[1])
    try:
        c, _ = s.accept()
        c.close()
    except socket.timeout:
        pass
"""

_STUBS = """
info()  { echo "INFO: $*"; }
warn()  { echo "WARN: $*"; }
error() { echo "ERROR: $*"; exit 1; }
"""


# --------------------------------------------------------------------------------------
# Source slicing
# --------------------------------------------------------------------------------------


def _text() -> str:
    return SCRIPT.read_text()


def _between(start: str, end: str) -> str:
    text = _text()
    a = text.index(start)
    b = text.index(end, a)
    return text[a:b]


def _supervision_slice() -> str:
    """The supervision block: state arrays, helpers, state machine, supervisor, cleanup."""
    block = _between("# --- Supervision", "# Log directory: a fresh 0700")
    for marker in ("_spawn_forward() {", "_forward_check() {", "_supervise() {", "cleanup() {"):
        assert marker in block, f"supervision slice is missing {marker}"
    return block


def _spec_loop_slice() -> str:
    """The loop turning PF_SPECS into registered, spawned forwards (skip logic included)."""
    match = re.search(r'^for spec in "\$\{PF_SPECS\[@\]\}"; do\n.*?^done\n', _text(), re.S | re.M)
    assert match, "PF_SPECS loop not found"
    return match.group(0)


def _startup_tail_slice() -> str:
    """No-services guard, startup wait + report, zero-active guard."""
    block = _between("# Skipped specs are not registered", 'info "Leave this running')
    assert "_wait_for_startup" in block and "_report_startup" in block
    return block


def _logdir_slice() -> str:
    block = _between("# Log directory: a fresh 0700", "# Signals: the loop")
    assert "LOG_DIR_ARG" in block
    return block


# --------------------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _port_answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _wait_for(pred: Callable[[], object], timeout: float = WAIT_SECS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return bool(pred())


class _Squatter:
    """Something else already holding a port: listens, never speaks (a leftover process)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.sock: socket.socket | None = None

    def __enter__(self) -> _Squatter:
        self.hold()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def hold(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", self.port))
        sock.listen(16)
        self.sock = sock

    def release(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


class _Proc:
    """A running harness with its output redirected to a file (no pipe for children to hold)."""

    def __init__(self, env: _PfEnv, script: str, extra_env: dict[str, str]) -> None:
        self.env = env
        self.out_path = env.tmp / f"out-{len(list(env.tmp.glob('out-*')))}.txt"
        self._out = self.out_path.open("w")
        self.started = time.monotonic()

        def _default_signals() -> None:
            # A pytest launched under nohup/&-job ignores SIGINT; a trap cannot override that.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

        self.popen = subprocess.Popen(
            [BASH, "-c", script, "bash"],
            stdout=self._out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env.environ(extra_env),
            cwd=ROOT,
            start_new_session=True,
            preexec_fn=_default_signals,  # noqa: PLW1509 - needed to undo inherited SIG_IGN
        )

    def output(self) -> str:
        return self.out_path.read_text()

    def wait_output(self, pattern: str, timeout: float = WAIT_SECS) -> bool:
        return _wait_for(lambda: re.search(pattern, self.output()) is not None, timeout)

    def finish(self, timeout: float = 90) -> int:
        try:
            return self.popen.wait(timeout=timeout)
        finally:
            self._out.close()

    def stop(self) -> None:
        if self.popen.poll() is None:
            self.popen.terminate()
            try:
                self.popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.popen.kill()
                self.popen.wait()
        if not self._out.closed:
            self._out.close()


class _PfEnv:
    """Per-test sandbox: stub kubectl on PATH, control dir, 0700 log dir."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.ctl = tmp / "ctl"
        self.logs = tmp / "logs"
        self.bin.mkdir()
        self.ctl.mkdir()
        self.logs.mkdir(mode=0o700)
        stub = self.bin / "kubectl"
        stub.write_text(_KUBECTL_STUB.format(python=sys.executable))
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.procs: list[_Proc] = []

    def environ(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("DATASPOKE_", "PORT_FORWARD_", "PF_"))
        }
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["PF_STUB_DIR"] = str(self.ctl)
        env["PF_TEST_LOGDIR"] = str(self.logs)
        env["PORT_FORWARD_POLL_SECS"] = "1"
        env["PORT_FORWARD_START_TIMEOUT_SECS"] = "10"
        env["PF_TEST_BACKOFF_MIN"] = "1"
        if extra:
            env.update(extra)
        return env

    # -- control --------------------------------------------------------------------
    def mode(self, port: int, mode: str) -> None:
        (self.ctl / f"mode-{port}").write_text(mode)

    def pid(self, port: int) -> int | None:
        path = self.ctl / f"pid-{port}"
        return int(path.read_text()) if path.exists() and path.read_text() else None

    def attempts(self, port: int) -> int:
        path = self.ctl / f"attempts-{port}"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def log(self, port: int) -> Path:
        return self.logs / f"pf-{port}.log"

    # -- harness --------------------------------------------------------------------
    def harness(self, body: str) -> str:
        return "\n".join(
            [
                "set -euo pipefail",
                f"source {shlex.quote(str(HELPERS))}",
                _STUBS,
                'LOG_DIR="$PF_TEST_LOGDIR"; LOG_DIR_OWNED=0; ENV_FILE=/test/.env.dev',
                _supervision_slice(),
                'BACKOFF_MIN_SECS="${PF_TEST_BACKOFF_MIN:-3}"',
                "trap cleanup EXIT",
                body,
            ]
        )

    def start(self, body: str, **extra_env: str) -> _Proc:
        proc = _Proc(self, self.harness(body), extra_env)
        self.procs.append(proc)
        return proc

    def run(self, body: str, **extra_env: str) -> tuple[int, str]:
        proc = self.start(body, **extra_env)
        rc = proc.finish()
        return rc, proc.output()

    def startup_body(self, specs: list[str], *, loop: bool = False) -> str:
        """Spec loop + startup tail (as in the script), optionally followed by the supervisor."""
        lines = [
            "PF_SPECS=(" + " ".join(shlex.quote(s) for s in specs) + ")",
            _spec_loop_slice(),
            _startup_tail_slice(),
            'echo "STARTUP_DONE active=$PF_ACTIVE"',
        ]
        if loop:
            lines += [
                "trap cleanup EXIT",
                "trap 'exit 130' INT",
                "trap 'exit 143' TERM",
                "_supervise",
            ]
        return "\n".join(lines)

    def teardown(self) -> None:
        for proc in self.procs:
            proc.stop()
        for path in self.ctl.glob("pid-*"):
            try:
                pid = int(path.read_text())
            except ValueError:
                continue
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.fixture
def pf(tmp_path: Path) -> Iterator[_PfEnv]:
    env = _PfEnv(tmp_path)
    try:
        yield env
    finally:
        env.teardown()


def _spec(port: int, name: str = "svc") -> str:
    return f"{port}:ns/{name}:5432"


SERVICES = "ns/svc,ns/svc2,ns/svc3"


# --------------------------------------------------------------------------------------
# Static checks on the real script
# --------------------------------------------------------------------------------------


def test_script_no_longer_discards_forward_output() -> None:
    """Issue #157: the spawn must stop sending kubectl's stderr to /dev/null."""
    text = _text()
    assert ">/dev/null 2>&1 &" not in text
    spawn = re.search(r"kubectl port-forward[^\n]*\n[^\n]*", text)
    assert spawn, "forward spawn not found"
    assert "/dev/null" not in spawn.group(0)
    assert re.search(r">\"\$log\" 2>&1", text), "forward output must go to its log file"


def test_script_has_no_bash4_only_constructs() -> None:
    """spec §Port-forward supervision (Signals and portability): bash 3.2, no `wait -n`."""
    rc = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    code = "\n".join(ln for ln in _text().splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"\bwait\s+-n\b", code)
    assert not re.search(r"\b(declare|local|typeset)\s+-A\b", code)
    assert not re.search(r"\$\{[A-Za-z_]+(,,|\^\^)\}", code)


def test_help_documents_supervision_and_log_dir() -> None:
    """spec §Port-forward supervision (Logs): `--log-dir` is a documented flag."""
    run = subprocess.run(
        [BASH, str(SCRIPT), "--help"], capture_output=True, text=True, check=False, cwd=ROOT
    )
    assert run.returncode == 0, run.stderr
    assert "--log-dir" in run.stdout
    assert "PORT_FORWARD_POLL_SECS" in run.stdout
    assert "PORT_FORWARD_START_TIMEOUT_SECS" in run.stdout


def test_unknown_flag_and_bare_log_dir_are_rejected() -> None:
    bad = subprocess.run(
        [BASH, str(SCRIPT), "--no-such-flag"], capture_output=True, text=True, check=False
    )
    assert bad.returncode != 0
    assert "Unknown option" in bad.stdout + bad.stderr
    bare = subprocess.run(
        [BASH, str(SCRIPT), "--log-dir"], capture_output=True, text=True, check=False
    )
    assert bare.returncode != 0
    assert "--log-dir" in bare.stdout + bare.stderr


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


def test_backoff_starts_at_3s_doubles_and_is_capped_at_30s(pf: _PfEnv) -> None:
    """spec: capped exponential backoff, 3s doubling to a 30s ceiling."""
    body = 'for n in 1 2 3 4 5 6 20; do echo "B $n $(_backoff_secs $n)"; done'
    rc, out = pf.run(body, PF_TEST_BACKOFF_MIN="3")
    assert rc == 0, out
    delays = {int(m[0]): int(m[1]) for m in re.findall(r"^B (\d+) (\d+)$", out, re.M)}
    assert [delays[1], delays[2], delays[3]] == [3, 6, 12]
    ordered = [delays[n] for n in (1, 2, 3, 4, 5, 6, 20)]
    assert ordered == sorted(ordered)
    assert max(ordered) == 30 and delays[20] == 30


def test_forward_ready_needs_pid_and_exact_port_line(pf: _PfEnv) -> None:
    """spec: ready = process alive AND kubectl's `Forwarding from 127.0.0.1:<port>` line.

    The exact-port guard (92010 must not satisfy 9201) is a correctness property the spec's
    `<port>` wording implies rather than states.
    """
    body = "\n".join(
        [
            "_pf_register 9201 ns svc 5432",
            'log="$(_pf_log 0)"',
            "PF_PID[0]=$$",
            "printf 'Forwarding from 127.0.0.1:92010 -> 5432\\n' > \"$log\"",
            "if _forward_ready 0; then echo SUPERSET_PORT_READY;"
            " else echo SUPERSET_PORT_NOT_READY; fi",
            "printf 'Forwarding from 127.0.0.1:9201 -> 5432\\n' >> \"$log\"",
            "if _forward_ready 0; then echo LINE_READY; else echo LINE_NOT_READY; fi",
            'PF_PID[0]=""',
            "if _forward_ready 0; then echo NOPID_READY; else echo NOPID_NOT_READY; fi",
        ]
    )
    rc, out = pf.run(body)
    assert rc == 0, out
    assert "SUPERSET_PORT_NOT_READY" in out
    assert "LINE_READY" in out
    assert "NOPID_NOT_READY" in out
    assert "SUPERSET_PORT_READY" not in out.replace("SUPERSET_PORT_NOT_READY", "")
    assert "NOPID_READY" not in out.replace("NOPID_NOT_READY", "")


def test_failure_reason_is_one_sanitized_line_naming_the_bind_error(pf: _PfEnv) -> None:
    """spec: the failed line carries the last log line, sanitized because it is kubectl output."""
    log = pf.log(9201)
    log.write_text(
        "Unable to listen on port 9201: bind: address already in use\x1b[31m\r\n"
        "error: unable to listen on any of the requested ports: [{9201 5432}]\n"
    )
    body = "_pf_register 9201 ns svc 5432\nprintf '[%s]' \"$(_pf_last_line 0)\""
    rc, out = pf.run(body)
    assert rc == 0, out
    reason = re.search(r"\[(.*)\]", out, re.S)
    assert reason
    assert "address already in use" in reason.group(1)
    assert "\x1b" not in out and "\r" not in out and "\n" not in reason.group(1)


# --------------------------------------------------------------------------------------
# Log directory
# --------------------------------------------------------------------------------------


def _logdir_run(pf: _PfEnv, log_dir_arg: str, **extra: str) -> subprocess.CompletedProcess[str]:
    script = "\n".join(
        [
            "set -euo pipefail",
            f"source {shlex.quote(str(HELPERS))}",
            _STUBS,
            f"LOG_DIR_ARG={shlex.quote(log_dir_arg)}",
            _logdir_slice(),
            'echo "LOG_DIR=$LOG_DIR"',
        ]
    )
    return subprocess.run(
        [BASH, "-c", script, "bash"],
        capture_output=True,
        text=True,
        check=False,
        env=pf.environ(extra),
    )


def test_default_log_dir_is_a_fresh_0700_dir_under_tmpdir(pf: _PfEnv) -> None:
    """spec Logs: fresh mode-0700 directory from `mktemp -d`, honouring TMPDIR."""
    tmpdir = pf.tmp / "tmpdir"
    tmpdir.mkdir()
    run = _logdir_run(pf, "", TMPDIR=str(tmpdir))
    assert run.returncode == 0, run.stdout + run.stderr
    made = Path(re.search(r"^LOG_DIR=(.*)$", run.stdout, re.M).group(1))  # type: ignore[union-attr]
    assert made.parent.resolve() == tmpdir.resolve()
    assert made.name.startswith("dataspoke-port-forward.")
    assert stat.S_IMODE(made.stat().st_mode) == 0o700


def test_log_dir_flag_creates_a_private_directory(pf: _PfEnv) -> None:
    target = pf.tmp / "made" / "logs"
    run = _logdir_run(pf, str(target))
    assert run.returncode == 0, run.stdout + run.stderr
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_log_dir_symlink_and_open_permissions_are_refused(pf: _PfEnv) -> None:
    """spec Logs: --log-dir must be a real directory, not a symlink, not group/world-writable."""
    real = pf.tmp / "real"
    real.mkdir(mode=0o700)
    link = pf.tmp / "link"
    link.symlink_to(real)
    sym = _logdir_run(pf, str(link))
    assert sym.returncode != 0
    assert "symlink" in sym.stdout

    open_dir = pf.tmp / "open"
    open_dir.mkdir()
    open_dir.chmod(0o770)
    writable = _logdir_run(pf, str(open_dir))
    assert writable.returncode != 0
    assert "writable" in writable.stdout

    # Backstop: the same slice accepts a private directory, so the refusals above came from the
    # checks and not from a harness fault.
    ok = _logdir_run(pf, str(real))
    assert ok.returncode == 0, ok.stdout


# --------------------------------------------------------------------------------------
# Startup: verify before counting
# --------------------------------------------------------------------------------------


def test_healthy_forwards_are_all_active_with_logs_per_port(pf: _PfEnv) -> None:
    """spec Readiness / Failures: `N of M port-forward(s) active`, one pf-<port>.log each."""
    a, b = _free_port(), _free_port()
    body = pf.startup_body([_spec(a), _spec(b, "svc2")])
    rc, out = pf.run(body, PF_STUB_SERVICES=SERVICES)
    assert rc == 0, out
    assert "2 of 2 port-forward(s) active" in out
    assert "FAILED" not in out
    for port in (a, b):
        assert re.search(rf"127\.0\.0\.1:{port} -> ns/\S+:5432 active \(pid \d+\)", out), out
        assert f"Forwarding from 127.0.0.1:{port}" in pf.log(port).read_text()
        assert stat.S_IMODE(pf.log(port).stat().st_mode) == 0o600


def test_port_answering_without_readiness_line_is_not_active(pf: _PfEnv) -> None:
    """spec Readiness: a forward counts only on kubectl's bind line, never on a TCP connect.

    `delayline` binds at once but prints the line 1.5s later, so for that window the port
    answers while the forward must still be `starting`.
    """
    port = _free_port()
    pf.mode(port, "delayline")
    body = "\n".join(
        [
            f"_pf_register {port} ns svc 5432",
            "_spawn_forward 0",
            "command sleep 0.6",
            "_forward_check 0",
            f"if _tcp_open {port}; then echo PORT_ANSWERS; else echo PORT_SILENT; fi",
            'echo "EARLY_STATE=${PF_STATE[0]}"',
            "_wait_for_startup",
            'echo "FINAL_STATE=${PF_STATE[0]}"',
        ]
    )
    rc, out = pf.run(body)
    assert rc == 0, out
    assert "PORT_ANSWERS" in out  # backstop: the connect probe would have said "ready"
    assert "EARLY_STATE=starting" in out
    assert "FINAL_STATE=up" in out


def test_squatted_port_is_reported_failed_and_not_counted(pf: _PfEnv) -> None:
    """Issue #157 acceptance: a port something else holds is FAILED, with the reason, uncounted."""
    good, bad = _free_port(), _free_port()
    with _Squatter(bad):
        rc, out = pf.run(
            pf.startup_body([_spec(good), _spec(bad, "svc2")]), PF_STUB_SERVICES=SERVICES
        )
        assert _port_answers(bad)  # backstop: the squatter really answers, as in the issue
    assert rc == 0, out
    assert "1 of 2 port-forward(s) active" in out
    failed = [ln for ln in out.splitlines() if "FAILED" in ln]
    assert len(failed) == 1, out
    assert f"127.0.0.1:{bad} -> ns/svc2" in failed[0]
    assert "address already in use" in failed[0]
    assert str(pf.log(bad)) in failed[0]
    assert re.search(rf"127\.0\.0\.1:{good} -> .* active \(pid", out)
    assert not re.search(rf"127\.0\.0\.1:{bad} -> .* active", out)


def test_no_active_forward_at_startup_is_fatal_and_names_the_log_dir(pf: _PfEnv) -> None:
    """spec: zero active at startup exits non-zero and names the log directory."""
    a, b = _free_port(), _free_port()
    with _Squatter(a), _Squatter(b):
        rc, out = pf.run(pf.startup_body([_spec(a), _spec(b, "svc2")]), PF_STUB_SERVICES=SERVICES)
    assert rc != 0, out
    assert "0 of 2 port-forward(s) active" in out
    assert re.search(r"ERROR: .*" + re.escape(str(pf.logs)), out), out
    assert "STARTUP_DONE" not in out


def test_skipped_specs_are_not_counted_as_failed(pf: _PfEnv) -> None:
    """spec Failures: specs skipped for an empty namespace or missing Service are not failures."""
    ok, no_ns, missing = _free_port(), _free_port(), _free_port()
    specs = [_spec(ok), f"{no_ns}:/svc2:5432", _spec(missing, "not-installed")]
    rc, out = pf.run(pf.startup_body(specs), PF_STUB_SERVICES=SERVICES)
    assert rc == 0, out
    assert "1 of 1 port-forward(s) active" in out
    assert "FAILED" not in out
    assert f"skip 127.0.0.1:{no_ns}" in out
    assert f"skip 127.0.0.1:{missing}" in out


def test_nothing_to_forward_keeps_its_own_error(pf: _PfEnv) -> None:
    port = _free_port()
    rc, out = pf.run(pf.startup_body([f"{port}:/svc:5432"]), PF_STUB_SERVICES=SERVICES)
    assert rc != 0
    assert "No services found to forward" in out


def test_bash32_empty_state_arrays_do_not_trip_set_u(pf: _PfEnv) -> None:
    """spec Signals and portability: runs on bash 3.2; an empty array must not abort under -u."""
    body = "\n".join(
        [
            "_wait_for_startup",
            "_report_startup",
            'echo "ACTIVE=$PF_ACTIVE"',
            "cleanup",
            "echo SURVIVED",
        ]
    )
    rc, out = pf.run(body)
    assert rc == 0, out
    assert "unbound variable" not in out
    assert "ACTIVE=0" in out and "SURVIVED" in out


def test_foreign_listener_does_not_satisfy_the_no_bind_line_fallback(pf: _PfEnv) -> None:
    """spec Readiness: with no bind line, a live process is accepted only if the listener on the
    port is its own socket; a foreign listener is reported failed, never guessed at.

    The stub stays alive, binds nothing and prints nothing; a squatter answers on the port.
    """
    port = _free_port()
    pf.mode(port, "idle")
    with _Squatter(port):
        rc, out = pf.run(
            pf.startup_body([_spec(port)]),
            PF_STUB_SERVICES=SERVICES,
            PORT_FORWARD_START_TIMEOUT_SECS="2",
        )
        assert _port_answers(port)
    # No active forward -> the zero-active guard exits non-zero.
    assert rc != 0, out
    assert "0 of 1 port-forward(s) active" in out
    assert re.search(rf"127\.0\.0\.1:{port} -> ns/svc FAILED: not ready after 2s", out), out
    assert "active (pid" not in out


@pytest.mark.skipif(not HAVE_LSOF, reason="listener-ownership fallback needs lsof")
def test_own_listener_without_bind_line_is_accepted_and_labelled(pf: _PfEnv) -> None:
    """spec Readiness fallback: a kubectl that prints no line is accepted after the start timeout
    when its own port answers, and the report says the confirmation was weaker."""
    port = _free_port()
    pf.mode(port, "noline")
    rc, out = pf.run(
        pf.startup_body([_spec(port)]),
        PF_STUB_SERVICES=SERVICES,
        PORT_FORWARD_START_TIMEOUT_SECS="2",
    )
    assert rc == 0, out
    assert "1 of 1 port-forward(s) active" in out
    assert "listener ownership" in out
    assert "FAILED" not in out


# --------------------------------------------------------------------------------------
# Log rotation and safe creation
# --------------------------------------------------------------------------------------


def test_respawn_rotates_the_log_and_never_follows_a_planted_symlink(pf: _PfEnv) -> None:
    """spec Logs: each log is created fresh and private, never following a pre-existing link;
    each spawn rotates the previous file to pf-<port>.log.prev."""
    port = _free_port()
    victim = pf.tmp / "victim"
    victim.write_text("precious\n")
    pf.log(port).symlink_to(victim)
    body = "\n".join(
        [
            f"_pf_register {port} ns svc 5432",
            "_spawn_forward 0",
            "_wait_for_startup",
            'echo "FIRST=${PF_STATE[0]}"',
            "_forward_kill 0",
            "_spawn_forward 0",
            "_wait_for_startup",
            'echo "SECOND=${PF_STATE[0]}"',
        ]
    )
    rc, out = pf.run(body)
    assert rc == 0, out
    assert "FIRST=up" in out and "SECOND=up" in out
    assert victim.read_text() == "precious\n"
    log = pf.log(port)
    assert not log.is_symlink() and stat.S_IMODE(log.stat().st_mode) == 0o600
    prev = Path(f"{log}.prev")
    assert prev.exists()
    assert f"Forwarding from 127.0.0.1:{port}" in prev.read_text()
    assert f"Forwarding from 127.0.0.1:{port}" in log.read_text()


# --------------------------------------------------------------------------------------
# Supervision loop
# --------------------------------------------------------------------------------------


def _start_loop(pf: _PfEnv, ports: list[int], **extra: str) -> _Proc:
    specs = [_spec(p, f"svc{'' if i == 0 else i + 1}") for i, p in enumerate(ports)]
    proc = pf.start(pf.startup_body(specs, loop=True), PF_STUB_SERVICES=SERVICES, **extra)
    assert proc.wait_output(r"STARTUP_DONE"), proc.output()
    return proc


def test_killed_child_is_logged_and_respawned(pf: _PfEnv) -> None:
    """Issue #157 acceptance: SIGKILL a forward and the supervisor logs the loss and the port
    answers again under a new pid."""
    port = _free_port()
    proc = _start_loop(pf, [port])
    old = pf.pid(port)
    assert old is not None and _port_answers(port)  # backstop: it was up before the kill
    os.kill(old, signal.SIGKILL)

    assert proc.wait_output(rf"127\.0\.0\.1:{port} -> ns/svc LOST: kubectl exited"), proc.output()
    assert proc.wait_output(r"recovered \(pid \d+\)"), proc.output()
    new = pf.pid(port)
    assert new is not None and new != old and _alive(new)
    assert _wait_for(lambda: _port_answers(port))
    assert pf.attempts(port) == 2
    assert len(re.findall(r"LOST", proc.output())) == 1
    assert len(re.findall(r"recovered", proc.output())) == 1
    # timestamped (HH:MM:SS) per spec
    assert re.search(r"WARN: \d\d:\d\d:\d\d 127\.0\.0\.1:", proc.output())


def test_stale_but_alive_forward_is_killed_and_respawned(pf: _PfEnv) -> None:
    """spec Supervision loop: stale = log gains `lost connection to pod` while the process is
    still alive; it is killed, logged once, and respawned."""
    port = _free_port()
    pf.mode(port, "stale")  # one-shot: the respawn behaves
    proc = _start_loop(pf, [port])
    old = pf.pid(port)
    assert old is not None

    assert proc.wait_output(rf"127\.0\.0\.1:{port} -> ns/svc LOST: stale forward"), proc.output()
    assert proc.wait_output(r"recovered \(pid \d+\)"), proc.output()
    new = pf.pid(port)
    assert new is not None and new != old
    assert _wait_for(lambda: not _alive(old)), "the stale process must have been killed"
    assert _wait_for(lambda: _port_answers(port))


def test_per_connection_errors_do_not_restart_a_healthy_forward(pf: _PfEnv) -> None:
    """spec Supervision loop: `Handling connection`, `error forwarding port` and `an error
    occurred forwarding` are per-connection lines and are deliberately NOT stale triggers."""
    port = _free_port()
    pf.mode(port, "chatter")
    proc = _start_loop(pf, [port])
    old = pf.pid(port)
    assert old is not None
    # Backstop: the chatter was really written to the log, then give the supervisor polls to act.
    assert _wait_for(lambda: "an error occurred forwarding" in pf.log(port).read_text())
    time.sleep(3.5)  # > 3 polls at PORT_FORWARD_POLL_SECS=1

    assert pf.pid(port) == old and _alive(old)
    assert pf.attempts(port) == 1
    assert "LOST" not in proc.output()


def test_persistent_failure_logs_once_then_recovers_when_the_port_frees(pf: _PfEnv) -> None:
    """spec Supervision loop: repeated failed retries are silent; a recovery logs one line.

    The squatted port is reported FAILED once at startup, retried (several spawn attempts, shown
    by the stub's attempt count) without another failure line, and comes up on its own once the
    squatter lets go.
    """
    good, bad = _free_port(), _free_port()
    squatter = _Squatter(bad)
    squatter.hold()
    try:
        proc = _start_loop(pf, [good, bad])
        assert len([ln for ln in proc.output().splitlines() if "FAILED" in ln]) == 1
        assert _wait_for(lambda: pf.attempts(bad) >= 4), "supervisor must keep retrying"
        out = proc.output()
        assert len([ln for ln in out.splitlines() if "FAILED" in ln or "LOST" in ln]) == 1, out
        assert "recovered" not in out
        assert pf.attempts(good) == 1  # the healthy forward was left alone
    finally:
        squatter.release()

    assert proc.wait_output(rf"127\.0\.0\.1:{bad} -> ns/svc2 recovered \(pid \d+\)"), proc.output()
    assert _wait_for(lambda: _port_answers(bad))
    final = proc.output()
    assert len(re.findall(r"recovered", final)) == 1
    assert len([ln for ln in final.splitlines() if "FAILED" in ln or "LOST" in ln]) == 1


def test_supervisor_survives_a_dead_child_under_set_e(pf: _PfEnv) -> None:
    """The supervisor runs under `set -euo pipefail`; a dead child (failing `kill -0`) must not
    abort it. Two forwards: killing one must leave the other polled and the loop alive."""
    a, b = _free_port(), _free_port()
    proc = _start_loop(pf, [a, b])
    victim = pf.pid(a)
    assert victim is not None
    os.kill(victim, signal.SIGKILL)
    assert proc.wait_output(r"recovered \(pid \d+\)"), proc.output()
    assert proc.popen.poll() is None, proc.output()
    assert _alive(pf.pid(b) or 0)


# --------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)], ids=["SIGINT", "SIGTERM"]
)
def test_signal_exits_promptly_and_leaves_no_child(
    pf: _PfEnv, sig: signal.Signals, code: int
) -> None:
    """spec Signals: SIGINT/SIGTERM exit promptly (even mid-poll-sleep) and leave no kubectl
    child behind; logs are retained and named."""
    a, b = _free_port(), _free_port()
    proc = _start_loop(pf, [a, b], PORT_FORWARD_POLL_SECS="30")
    pids = [pf.pid(a), pf.pid(b)]
    assert all(p is not None and _alive(p) for p in pids)
    time.sleep(0.5)  # let the supervisor enter its 30s nap

    sent = time.monotonic()
    proc.popen.send_signal(sig)
    rc = proc.finish(timeout=10)
    assert time.monotonic() - sent < 5, "must not wait out the 30s poll sleep"
    assert rc == code
    assert _wait_for(lambda: not any(_alive(p) for p in pids if p is not None), 5)
    assert "Port-forward logs kept in" in proc.output()
    assert pf.log(a).exists() and pf.log(b).exists()


def test_signal_after_a_respawn_kills_the_current_pid(pf: _PfEnv) -> None:
    """spec Signals: the forwards running at that moment, including respawned ones, are killed."""
    port = _free_port()
    proc = _start_loop(pf, [port])
    old = pf.pid(port)
    assert old is not None
    os.kill(old, signal.SIGKILL)
    assert proc.wait_output(r"recovered \(pid \d+\)"), proc.output()
    new = pf.pid(port)
    assert new is not None and new != old and _alive(new)

    proc.popen.send_signal(signal.SIGTERM)
    assert proc.finish(timeout=10) == 143
    assert _wait_for(lambda: not _alive(new), 5)


# --------------------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "abc", "-3", "2.5"])
def test_malformed_tunable_is_rejected_up_front(pf: _PfEnv, bad: str) -> None:
    """Untraced to a spec line (the spec names the knobs, not their validation): a non-integer
    value must stop the script at startup, not abort the arithmetic mid-supervision."""
    proc = pf.start("echo NOT_REACHED", PORT_FORWARD_POLL_SECS=bad)
    rc = proc.finish()
    assert rc != 0
    assert "Invalid PORT_FORWARD_POLL_SECS" in proc.output()
    assert "NOT_REACHED" not in proc.output()

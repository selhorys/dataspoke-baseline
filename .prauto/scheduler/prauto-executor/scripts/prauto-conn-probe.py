#!/usr/bin/env python3
"""Connectivity probe for a live prauto Stage 3 run (read-only, side channel).

While the executor's integration groups run, sample the exact path the tests use
(laptop -> dev-env TCP host) so a failure can be attributed to the transport
instead of the branch:

  * SYN probe   : TCP connect to the dev-env postgres/redis/lock ports.
  * HOLD probe  : keep one idle TCP session open to postgres and detect a
                  mid-operation close (the `connection was closed in the middle
                  of operation` signature) without sending any protocol bytes.
  * QUERY probe : a real `psql -c 'select 1'`, i.e. the same client the tests
                  shell out to (tests/integration/spot/test_peripheral_links.py).

Log lines are timestamped and written to stdout; the probe exits on a stop file,
on the deadline, or when its parent (the executor) disappears.

  usage: prauto-conn-probe.py [HOST [PG_PORT [REDIS_PORT [LOCK_PORT [SECS]]]]]

Defaults come from the dev-env env vars when present (source helm-charts/.env.dev
first), so this tracks the current cluster instead of a stale address:

  DATASPOKE_DEV_POSTGRES_HOST/PORT, DATASPOKE_DEV_REDIS_HOST/PORT,
  DATASPOKE_DEV_LOCK_URL.
Output: stdout — launch it through .prauto/scheduler/daemonize.py LOGFILE -- ...
Stop:   $PRAUTO_PROBE_STOP (default /tmp/prauto-probe/stop)
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse


def _env_port(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _lock_port(default: int) -> int:
    url = os.environ.get("DATASPOKE_DEV_LOCK_URL", "")
    try:
        return urllib.parse.urlsplit(url).port or default
    except ValueError:
        return default


def _arg(i: int) -> str:
    """Positional arg i, treating an empty string as 'use the default'."""
    return sys.argv[i] if len(sys.argv) > i and sys.argv[i] else ""


HOST = _arg(1) or os.environ.get("DATASPOKE_DEV_POSTGRES_HOST", "")
PG_PORT = int(_arg(2) or _env_port("DATASPOKE_DEV_POSTGRES_PORT", 9201))
REDIS_PORT = int(_arg(3) or _env_port("DATASPOKE_DEV_REDIS_PORT", 9202))
LOCK_PORT = int(_arg(4) or _lock_port(9221))
SECS = int(_arg(5) or 7200)

if not HOST:
    sys.exit("ERROR: no host — pass HOST, or export DATASPOKE_DEV_POSTGRES_HOST "
             "(set -a && source helm-charts/.env.dev && set +a).")

LOG = "/tmp/prauto-probe"  # scratch dir only; log lines go to stdout (the daemonizer's LOGFILE)
STOP = os.environ.get("PRAUTO_PROBE_STOP", "/tmp/prauto-probe/stop")
PSQL = shutil.which("psql")


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def probe_once() -> None:
    for label, port in (("postgres", PG_PORT), ("redis", REDIS_PORT), ("lock", LOCK_PORT)):
        t0 = time.time()
        try:
            with socket.create_connection((HOST, port), timeout=3):
                log(f"SYN  {label:<8} {HOST}:{port:<5} ok      {(time.time()-t0)*1000:6.0f}ms")
        except OSError as exc:
            log(f"SYN  {label:<8} {HOST}:{port:<5} FAIL    {(time.time()-t0)*1000:6.0f}ms  {exc!r}")


def hold_cycle(deadline: float) -> None:
    """One idle session held open; report how it ended."""
    opened = time.time()
    try:
        sock = socket.create_connection((HOST, PG_PORT), timeout=5)
    except OSError as exc:
        log(f"HOLD {HOST}:{PG_PORT} connect FAIL  {exc!r}")
        return
    sock.settimeout(5)
    while time.time() < deadline and not os.path.exists(STOP):
        try:
            data = sock.recv(1)
        except socket.timeout:
            continue
        except OSError as exc:
            log(f"HOLD {HOST}:{PG_PORT} closed after {int(time.time()-opened)}s  {exc!r}")
            sock.close()
            return
        if data == b"":
            log(f"HOLD {HOST}:{PG_PORT} closed after {int(time.time()-opened)}s  peer EOF")
            sock.close()
            return
        sock.recv(4096)
    sock.close()
    log(f"HOLD {HOST}:{PG_PORT} ended cleanly after {int(time.time()-opened)}s")


def query_once() -> None:
    if not PSQL:
        return
    cmd = [
        PSQL, f"--host={HOST}", f"--port={PG_PORT}",
        "--username=dataspoke", "--dbname=dataspoke", "--no-password",
        "--set=ON_ERROR_STOP=1", "--command=select 1",
    ]
    t0 = time.time()
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=20, text=True)
    except subprocess.TimeoutExpired:
        log("QUERY psql select 1 TIMEOUT after 20s")
        return
    if done.returncode == 0:
        log(f"QUERY psql select 1 ok      {(time.time()-t0)*1000:6.0f}ms")
    else:
        msg = (done.stderr or "").strip().splitlines()
        log(f"QUERY psql select 1 FAIL rc={done.returncode}  {msg[-1] if msg else ''}")


def main() -> int:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if os.path.exists(STOP):
        os.unlink(STOP)
    deadline = time.time() + SECS
    log(f"probe start host={HOST} pg={PG_PORT} redis={REDIS_PORT} lock={LOCK_PORT} secs={SECS}")
    tick = 0
    hold_deadline = time.time() + 90
    while time.time() < deadline and not os.path.exists(STOP):
        probe_once()
        tick += 1
        if tick % 6 == 0:
            query_once()
        if time.time() >= hold_deadline:
            hold_cycle(hold_deadline)  # blocks up to the 90s window
            hold_deadline = time.time() + 90
        time.sleep(5)
    log("probe end")
    return 0


if __name__ == "__main__":
    sys.exit(main())

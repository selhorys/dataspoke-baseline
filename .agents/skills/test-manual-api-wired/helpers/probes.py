#!/usr/bin/env python3
"""Side-effect probes for test-manual-api-wired.

Reads /tmp/_manual_test_env, runs one probe, prints findings as JSON to stdout.
Exits non-zero only on infra failure (DB unreachable, GMS down, etc.) — never
on assertion mismatch. The skill interprets the JSON to decide pass/fail.

Usage:
    probes.py --list
    probes.py db_row <table> <urn>
    probes.py db_count <table> [where_clause]
    probes.py events_passive_count <urn>
    probes.py events_window <urn> <since_iso> [event_type_prefix]
    probes.py gms_aspect <urn> <aspect>
    probes.py gms_systemmetadata <urn> <aspect>
    probes.py gms_lastingested <urn>
    probes.py k8s_secret <name> [namespace]
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request


ENV_FILE = pathlib.Path("/tmp/_manual_test_env")


def _load_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        sys.exit(f"ERROR: {ENV_FILE} not found. Run setup_env.sh first.")
    out: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _http(method: str, url: str, *, headers: dict[str, str] | None = None,
          body: bytes | None = None, timeout: float = 15.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _pg(env: dict[str, str], db_role: str, sql: str) -> str:
    """Run a one-shot psql against either source PG or DataSpoke PG.

    db_role: "source" | "dataspoke"
    """
    if db_role == "source":
        host = env["PG_HOST"]
        port = env["PG_PORT"]
        user = env["PG_USER"]
        pwd = env["PG_PASSWORD"]
        db = env["PG_DB"]
    elif db_role == "dataspoke":
        host = env["DATASPOKE_PG_HOST"]
        port = env["DATASPOKE_PG_PORT"]
        user = env["DATASPOKE_PG_USER"]
        pwd = env["DATASPOKE_PG_PASSWORD"]
        db = env["DATASPOKE_PG_DB"]
    else:
        sys.exit(f"ERROR: unknown db_role={db_role!r}")
    cmd = ["psql", "-h", host, "-p", port, "-U", user, "-d", db,
           "-At", "-F", "\t", "-c", sql]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         env={**os.environ, "PGPASSWORD": pwd}, timeout=15)
    if res.returncode != 0:
        sys.exit(f"ERROR: psql failed: {res.stderr.strip()}")
    return res.stdout


# ── Probes ──────────────────────────────────────────────────────────────────

PROBES: list[tuple[str, str]] = []


def _probe(arg_help: str):
    def deco(fn):
        PROBES.append((fn.__name__, arg_help))
        return fn
    return deco


@_probe("<table> <urn>  — fetch one row from dataspoke.<table> by dataset_urn")
def db_row(table: str, urn: str) -> dict:
    env = _load_env()
    sql = (
        f"SELECT row_to_json(t) FROM dataspoke.{table} t "
        f"WHERE dataset_urn = $${urn}$$ LIMIT 1;"
    )
    out = _pg(env, "dataspoke", sql).strip()
    return {"table": table, "urn": urn, "row": json.loads(out) if out else None,
            "found": bool(out)}


@_probe("<table> [where_sql]  — count rows in dataspoke.<table>")
def db_count(table: str, where: str = "TRUE") -> dict:
    env = _load_env()
    sql = f"SELECT count(*) FROM dataspoke.{table} WHERE {where};"
    out = _pg(env, "dataspoke", sql).strip()
    return {"table": table, "where": where, "count": int(out)}


@_probe("<urn>  — count INGESTION.COMPLETE+source=passive events for URN")
def events_passive_count(urn: str) -> dict:
    env = _load_env()
    sql = (
        "SELECT count(*) FROM dataspoke.events "
        f"WHERE entity_id = $${urn}$$ "
        "AND event_type IN ('INGESTION.COMPLETE','INGESTION.FAIL') "
        "AND detail->>'source' = 'passive';"
    )
    return {"urn": urn, "passive_count": int(_pg(env, "dataspoke", sql).strip())}


@_probe("<urn> <since_iso> [event_type_prefix]  — events for URN since timestamp")
def events_window(urn: str, since_iso: str, prefix: str = "") -> dict:
    env = _load_env()
    where = f"entity_id = $${urn}$$ AND occurred_at > '{since_iso}'"
    if prefix:
        where += f" AND event_type LIKE '{prefix}%'"
    sql = (
        "SELECT json_agg(row_to_json(t)) FROM (SELECT event_type, status, "
        "detail, occurred_at FROM dataspoke.events "
        f"WHERE {where} ORDER BY occurred_at DESC) t;"
    )
    out = _pg(env, "dataspoke", sql).strip()
    rows = json.loads(out) if out and out != "" else []
    return {"urn": urn, "since": since_iso, "count": len(rows or []),
            "rows": rows or []}


@_probe("<urn> <aspect>  — fetch a DataHub aspect (unwrapped, no FQCN navigation)")
def gms_aspect(urn: str, aspect: str) -> dict:
    env = _load_env()
    enc = urllib.parse.quote(urn, safe="")
    headers = {"Authorization": f"Bearer {env['GMS_TOKEN']}"} if env.get("GMS_TOKEN") else {}
    sc, body = _http("GET", f"{env['GMS']}/aspects/{enc}?aspect={aspect}&version=0",
                     headers=headers)
    if sc == 404:
        return {"urn": urn, "aspect": aspect, "status": sc, "found": False, "value": None}
    if sc != 200:
        return {"urn": urn, "aspect": aspect, "status": sc, "error": body}
    # GMS envelope: {"version": 0, "aspect": {"<FQCN>": {...}}} — strip the FQCN layer
    # so callers don't need to know each aspect's com.linkedin.* class path.
    payload = json.loads(body)
    inner = payload.get("aspect", {})
    value = next(iter(inner.values()), None) if isinstance(inner, dict) else None
    return {"urn": urn, "aspect": aspect, "status": sc, "found": value is not None,
            "value": value}


@_probe("<urn> <aspect>  — fetch systemMetadata.runId/lastObserved for an aspect")
def gms_systemmetadata(urn: str, aspect: str) -> dict:
    env = _load_env()
    enc = urllib.parse.quote(urn, safe="")
    headers = {"Authorization": f"Bearer {env['GMS_TOKEN']}"} if env.get("GMS_TOKEN") else {}
    sc, body = _http(
        "GET",
        f"{env['GMS']}/openapi/v3/entity/dataset/{enc}?systemMetadata=true&aspects={aspect}",
        headers=headers,
    )
    if sc != 200:
        return {"urn": urn, "aspect": aspect, "status": sc, "error": body}
    sm = json.loads(body).get(aspect, {}).get("systemMetadata", {})
    return {"urn": urn, "aspect": aspect, "runId": sm.get("runId"),
            "lastObserved": sm.get("lastObserved")}


@_probe("<urn>  — GraphQL fetch of dataset.lastIngested")
def gms_lastingested(urn: str) -> dict:
    env = _load_env()
    headers = {"Content-Type": "application/json"}
    if env.get("GMS_TOKEN"):
        headers["Authorization"] = f"Bearer {env['GMS_TOKEN']}"
    payload = json.dumps({
        "query": "query getLastIngested($urn: String!) { dataset(urn: $urn) { lastIngested } }",
        "variables": {"urn": urn},
    }).encode()
    sc, body = _http("POST", f"{env['GMS']}/api/graphql", headers=headers, body=payload)
    if sc != 200:
        return {"urn": urn, "status": sc, "error": body}
    li = json.loads(body).get("data", {}).get("dataset", {}).get("lastIngested")
    return {"urn": urn, "lastIngested": li}


@_probe("<name> [namespace]  — read+decode a k8s Secret")
def k8s_secret(name: str, namespace: str | None = None) -> dict:
    env = _load_env()
    ns = namespace or env["DATASPOKE_K8S_NAMESPACE"]
    res = subprocess.run(
        ["kubectl", "get", "secret", name, "-n", ns, "-o", "json"],
        capture_output=True, text=True, timeout=15,
    )
    if res.returncode != 0:
        return {"name": name, "namespace": ns, "found": False,
                "stderr": res.stderr.strip()}
    import base64
    obj = json.loads(res.stdout)
    decoded = {k: base64.b64decode(v).decode("utf-8", errors="replace")
               for k, v in obj.get("data", {}).items()}
    return {"name": name, "namespace": ns, "found": True,
            "created": obj["metadata"]["creationTimestamp"],
            "keys": list(decoded.keys()), "values": decoded}


def _list() -> None:
    print("Available probes:")
    for name, args in PROBES:
        print(f"  {name:32s} {args}")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("--list", "-l", "--help", "-h"):
        _list()
        return 0
    name, *rest = argv
    fn = next((p for p in PROBES if p[0] == name), None)
    if fn is None:
        print(f"ERROR: unknown probe {name!r}", file=sys.stderr)
        _list()
        return 2
    result = globals()[name](*rest)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

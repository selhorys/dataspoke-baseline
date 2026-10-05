"""Hermetic tests for the bounded teardown of `helm-charts/bin/uninstall.sh` (#158).

The REAL `uninstall.sh` runs under `/bin/bash` (3.2 on macOS) with fake `kubectl` and `helm`
executables first on PATH and a temp env file standing in for `helm-charts/.env.<profile>`. Both
fakes are thin front-ends over one small cluster model kept as JSON in a per-test state dir:

* a controller (StatefulSet / Deployment) recreates its pod when the pod is deleted, unless the
  controller itself is gone;
* deleting a controller garbage-collects its pods (and, for a Deployment, its ReplicaSets) except
  pods flagged `stuck` (stay terminating until force-deleted) or `unremovable` (survive even a
  force delete, as a finalizer would make them);
* `kubectl delete pvc` / `delete namespace` block until their `--timeout` while a pod still
  mounts the claim / is stuck in the namespace, then fail like kubectl does;
* every invocation is appended to a call log the tests assert on.

Nothing here talks to a cluster. The timeouts are 2-3 seconds (`DATASPOKE_UNINSTALL_*`), the
script's poll interval is its own `sleep 1`, and every run has a hard wall-clock limit.

Assertions derive from `spec/feature/HELM_CHART.md` §Uninstallation / §Bounded teardown and the
approved plan for #158, not from incidental implementation behaviour. Where a test pins wording
the spec does not fix, the docstring says so.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "helm-charts/bin/uninstall.sh"

# The macOS system bash (3.2) is the platform that rules out `wait -n`, mapfile and
# associative arrays; prefer it when present.
BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"

DELETE_SECS = 2  # DATASPOKE_UNINSTALL_DELETE_TIMEOUT_SECS used by most tests
RELEASE_SECS = 3  # DATASPOKE_UNINSTALL_RELEASE_TIMEOUT_SECS used by most tests
HARD_LIMIT_SECS = 150  # wall-clock limit for any one script run

CLUSTER = "fake-ctx"
NS = "ds-t"  # dataspoke namespace
DATAHUB_NS = "dh-t"
LANGFUSE_NS = "lf-t"
DUMMY_NS = "dummy-t"

POSTGRES_PVC = "data-dataspoke-postgresql-0"
POSTGRES_POD = "dataspoke-postgresql-0"
API_LABEL = "app.kubernetes.io/name=dataspoke-api"
UMBRELLA_LABELS = ("app.kubernetes.io/instance=dataspoke", "release=dataspoke,tier=airflow")


# --------------------------------------------------------------------------------------
# Fake cluster model (shared by the kubectl and helm stubs)
# --------------------------------------------------------------------------------------

_FAKE_MODULE = r"""
import json
import os
import sys
import time

D = os.environ["FAKE_CLUSTER_DIR"]
STATE = os.path.join(D, "state.json")
LOG = os.path.join(D, "calls.log")
WORKLOADS = ("statefulset", "deployment", "daemonset", "job", "cronjob")


def load():
    with open(STATE) as f:
        return json.load(f)


def save(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE)


def log(tool, args):
    with open(LOG, "a") as f:
        f.write(json.dumps({"tool": tool, "args": args, "t": time.time()}) + "\n")


def unhandled(tool, args):
    with open(os.path.join(D, "unhandled.log"), "a") as f:
        f.write(json.dumps({"tool": tool, "args": args}) + "\n")
    sys.stderr.write("fake %s: unhandled invocation %r\n" % (tool, args))
    sys.exit(2)


def parse(args, value_flags):
    pos, opts, flags, i = [], {}, set(), 0
    while i < len(args):
        a = args[i]
        if a in value_flags:
            opts[a] = args[i + 1]
            i += 2
            continue
        if a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            opts[k] = v
        elif a.startswith("-"):
            flags.add(a)
        else:
            pos.append(a)
        i += 1
    return pos, opts, flags


def matches(labels, selector):
    if not selector:
        return True
    for part in selector.split(","):
        k, _, v = part.partition("=")
        if labels.get(k) != v:
            return False
    return True


def new_uid(s):
    s["uid_counter"] += 1
    return "uid-%d" % s["uid_counter"]


def rs_owner(s, ns, name):
    for r in s["replicasets"]:
        if r["ns"] == ns and r["name"] == name:
            return r["owner"]
    return None


def owned_by(s, c, p):
    if p["ns"] != c["ns"]:
        return False
    for kind, name in p["owners"]:
        if (kind, name) == (c["kind"], c["name"]):
            return True
        if kind == "ReplicaSet" and rs_owner(s, c["ns"], name) == "%s/%s" % (c["kind"], c["name"]):
            return True
    return False


def spawn_pod(s, c, initial):
    if c["kind"] == "Deployment":
        rs = c.get("rs_name") or "%s-5d9f" % c["name"]
        if rs_owner(s, c["ns"], rs) is None:
            s["replicasets"].append({"ns": c["ns"], "name": rs, "owner": "Deployment/" + c["name"]})
        owners = [["ReplicaSet", rs]]
        name = "%s-p%d" % (rs, c["gen"])
    else:
        owners = [[c["kind"], c["name"]]]
        name = "%s-0" % c["name"]
    s["pods"].append({
        "name": name, "ns": c["ns"], "uid": new_uid(s),
        "labels": dict(c.get("pod_labels") or c["labels"]),
        "owners": owners, "claims": list(c["claims"]),
        "stuck": bool(c["stuck"]) if initial else False,
        "unremovable": bool(c["unremovable"]) if initial else False,
        "terminating": False,
    })


def reconcile(s):
    for c in list(s["controllers"]):
        if c["kind"] not in ("StatefulSet", "Deployment"):
            continue
        if not any(owned_by(s, c, p) and not p["terminating"] for p in s["pods"]):
            c["gen"] += 1
            spawn_pod(s, c, initial=False)
            s["recreations"] += 1


def remove_controller(s, c):
    s["controllers"].remove(c)
    ref = "%s/%s" % (c["kind"], c["name"])
    rs_names = {r["name"] for r in s["replicasets"] if r["ns"] == c["ns"] and r["owner"] == ref}
    s["replicasets"] = [
        r for r in s["replicasets"] if not (r["ns"] == c["ns"] and r["owner"] == ref)
    ]
    kept = []
    for p in s["pods"]:
        owned = p["ns"] == c["ns"] and any(
            (k, n) == (c["kind"], c["name"]) or (k == "ReplicaSet" and n in rs_names)
            for k, n in p["owners"]
        )
        if owned and (p["stuck"] or p["unremovable"]):
            p["terminating"] = True
            kept.append(p)
        elif not owned:
            kept.append(p)
    s["pods"] = kept
    for late in s["late_pods"].pop("%s/%s" % (c["ns"], c["name"]), []):
        s["pods"].append({
            "name": late["name"], "ns": c["ns"], "uid": new_uid(s), "labels": late["labels"],
            "owners": [["ReplicaSet", late["owner_rs"]]], "claims": [],
            "stuck": True, "unremovable": False, "terminating": True,
        })
        s["late_spawned"] += 1


def remove_namespace(s, target):
    for pvc in [p for p in s["pvcs"] if p["ns"] == target]:
        s["pvs"] = [v for v in s["pvs"] if v["name"] != pvc["pv"] or v["retain"]]
    for key in ("pods", "pvcs", "controllers", "replicasets", "secrets"):
        s[key] = [o for o in s[key] if o["ns"] != target]
    s["namespaces"].pop(target, None)


def mounted_by(s, ns, pvc):
    return [p for p in s["pods"] if p["ns"] == ns and pvc in p["claims"]]


def sleep_out(timeout):
    # An untimed blocking delete never returns on its own: the test's hard limit catches it.
    time.sleep(timeout if timeout is not None else 3600)


def timeout_secs(opts):
    v = opts.get("--timeout")
    return int(v.rstrip("s")) if v else None


# ------------------------------------------------------------------ kubectl


def kubectl_main():
    args = sys.argv[1:]
    log("kubectl", args)
    pos, opts, flags = parse(args, {"-n", "--namespace", "-l", "--selector", "-o", "-f"})
    ns = opts.get("-n") or opts.get("--namespace")
    selector = opts.get("-l") or opts.get("--selector")
    s = load()
    if "--raw" in opts:
        print("ok")
        return
    verb = pos[0] if pos else ""
    rest = pos[1:]
    if verb == "config":
        if rest[0] == "view":
            print("apiVersion: v1\nkind: Config")
        elif rest[0] == "use-context":
            s["context"] = rest[1]
            save(s)
            print('Switched to context "%s".' % rest[1])
        elif rest[0] == "current-context":
            print(s["context"] or "")
        else:
            unhandled("kubectl", args)
        return
    if verb == "get":
        kubectl_get(s, rest, opts, flags, ns, selector, args)
    elif verb == "delete":
        kubectl_delete(s, rest, opts, flags, ns, selector, args)
    else:
        unhandled("kubectl", args)
    save(s)


def not_found(what):
    sys.stderr.write('Error from server (NotFound): %s not found\n' % what)
    sys.exit(1)


def kubectl_get(s, rest, opts, flags, ns, selector, args):
    kind = rest[0]
    name = rest[1] if len(rest) > 1 else None
    if "/" in kind:
        kind, name = kind.split("/", 1)
    fmt = opts.get("-o", "")
    if kind in ("pods", "pod"):
        if ns in s["fail_get_pods"]:
            sys.stderr.write("Unable to connect to the server: net/http: TLS handshake timeout\n")
            sys.exit(1)
        for p in s["pods"]:
            if p["ns"] != ns or not matches(p["labels"], selector):
                continue
            if "ownerReferences" in fmt:
                owners = "".join("%s/%s," % (k, n) for k, n in p["owners"])
                print("%s|%s|%s" % (p["name"], owners, p["uid"]))
            else:
                claims = "".join(c + "," for c in p["claims"]) + ","
                print("%s|%s|%s" % (p["name"], claims, p["uid"]))
    elif kind == "replicaset":
        owner = rs_owner(s, ns, name)
        if owner is None:
            not_found("replicasets.apps %s" % name)
        print("%s|true" % owner)
    elif kind in ("pvc", "persistentvolumeclaim"):
        pvcs = [p for p in s["pvcs"] if p["ns"] == ns and matches(p["labels"], selector)]
        if name is None:
            print(" ".join(p["name"] for p in pvcs))
            return
        pvc = next((p for p in pvcs if p["name"] == name), None)
        if pvc is None:
            not_found("persistentvolumeclaims %s" % name)
        if "volumeName" in fmt:
            print(pvc["pv"] or "")
        elif "finalizers" in fmt:
            if mounted_by(s, ns, name):
                print('["kubernetes.io/pvc-protection"]')
        elif "storage" in fmt:
            print("8Gi")
    elif kind == "pv":
        if any(p["name"] == name for p in s["pvs"]):
            print("persistentvolume/" + name)
        elif "--ignore-not-found" not in flags and "--ignore-not-found" not in opts:
            not_found("persistentvolumes %s" % name)
    elif kind == "secret":
        if not any(x["ns"] == ns and x["name"] == name for x in s["secrets"]):
            not_found("secrets %s" % name)
    elif kind == "namespace":
        if fmt == "name":
            # The bounded presence / re-check read. Counted per namespace so a test can make
            # the namespace finish terminating, or the read fail, from the Nth read on.
            cfg = s["ns_cfg"].get(name, {})
            reads = s["ns_reads"].get(name, 0) + 1
            s["ns_reads"][name] = reads
            if cfg.get("vanish_after") is not None and reads > cfg["vanish_after"]:
                remove_namespace(s, name)
            if cfg.get("read_fail_from") and reads >= cfg["read_fail_from"]:
                save(s)
                sys.stderr.write("Unable to connect to the server: TLS handshake timeout\n")
                sys.exit(1)
            if name not in s["namespaces"]:
                if "--ignore-not-found" in flags:
                    return
                not_found("namespaces %s" % name)
            print("namespace/" + name)
            return
        if name not in s["namespaces"]:
            not_found("namespaces %s" % name)
        if "conditions" in fmt and s["namespaces"][name]["block"]:
            print(s["namespaces"][name]["block"])
    elif set(kind.split(",")) <= set(WORKLOADS):
        kinds = set(kind.split(","))
        items = [
            c for c in s["controllers"]
            if c["ns"] == ns and c["kind"].lower() in kinds and matches(c["labels"], selector)
            and (name is None or c["name"] == name)
        ]
        if name and not items:
            if "--ignore-not-found" in flags or "--ignore-not-found" in opts:
                return
            not_found("%s %s" % (kind, name))
        if fmt == "name":
            for c in items:
                print("%s.apps/%s" % (c["kind"].lower(), c["name"]))
    elif kind in ("service", "configmap", "ingress") and name:
        not_found("%s %s" % (kind, name))  # peripherals this model does not carry
    else:
        unhandled("kubectl", args)


def kubectl_delete(s, rest, opts, flags, ns, selector, args):
    secs = timeout_secs(opts)
    if "-f" in opts:
        return
    kind = rest[0]
    names = rest[1:]
    if "/" in kind:
        kind, one = kind.split("/", 1)
        names = [one]
    if kind in ("pod", "pods"):
        for n in names:
            pod = next((p for p in s["pods"] if p["ns"] == ns and p["name"] == n), None)
            forced = "--force" in flags or opts.get("--grace-period") == "0"
            # A stuck pod ignores a graceful delete (its node is gone); only force removes it.
            removable = not pod["unremovable"] and (forced or not pod["stuck"]) if pod else False
            if pod is not None and removable:
                s["pods"].remove(pod)
        reconcile(s)
    elif kind in ("pvc", "persistentvolumeclaim"):
        pvc = next((p for p in s["pvcs"] if p["ns"] == ns and p["name"] == names[0]), None)
        if pvc is None:
            return
        if mounted_by(s, ns, pvc["name"]):
            sleep_out(secs)
            sys.stderr.write(
                "error: timed out waiting for the condition on "
                "persistentvolumeclaims/%s\n" % pvc["name"]
            )
            sys.exit(1)
        s["pvcs"].remove(pvc)
        s["pvs"] = [v for v in s["pvs"] if v["name"] != pvc["pv"] or v["retain"]]
        print('persistentvolumeclaim "%s" deleted' % pvc["name"])
    elif kind == "namespace":
        target = names[0]
        if target not in s["namespaces"]:
            return
        blocked = s["namespaces"][target]["block"] or any(
            p["ns"] == target and (p["stuck"] or p["unremovable"]) for p in s["pods"]
        )
        if blocked:
            sleep_out(secs)
            sys.stderr.write(
                "error: timed out waiting for the condition on namespaces/%s\n" % target
            )
            sys.exit(1)
        remove_namespace(s, target)
        print('namespace "%s" deleted' % target)
    elif kind == "secret":
        s["secrets"] = [x for x in s["secrets"] if not (x["ns"] == ns and x["name"] == names[0])]
    elif set(kind.split(",")) <= set(WORKLOADS):
        kinds = set(kind.split(","))
        for c in [
            c for c in s["controllers"]
            if c["ns"] == ns and c["kind"].lower() in kinds
            and (
                ("--all" in flags)
                or (selector and matches(c["labels"], selector))
                or c["name"] in names
            )
        ]:
            remove_controller(s, c)
        reconcile(s)
    else:
        unhandled("kubectl", args)


# --------------------------------------------------------------------- helm


def helm_main():
    args = sys.argv[1:]
    log("helm", args)
    pos, opts, flags = parse(args, {"--namespace", "-n", "--timeout", "-o"})
    ns = opts.get("--namespace") or opts.get("-n")
    s = load()
    verb, name = pos[0], pos[1] if len(pos) > 1 else None
    key = "%s/%s" % (ns, name)
    rel = s["releases"].get(key)
    if verb == "status":
        if rel is None:
            sys.stderr.write("Error: release: not found\n")
            sys.exit(1)
        print("STATUS: deployed")
    elif verb == "get" and name == "values":
        rel = s["releases"].get("%s/%s" % (ns, pos[2]))
        if rel is None:
            sys.exit(1)
        print(json.dumps(rel["values"]))
    elif verb == "uninstall":
        if rel is None:
            sys.stderr.write(
                "Error: uninstall: Release not loaded: %s: release: not found\n" % name
            )
            sys.exit(1)
        if rel["uninstall"] == "timeout":
            if rel["drop_record"]:
                del s["releases"][key]
                save(s)
            sys.stderr.write(
                "Error: uninstallation completed with 1 error(s): "
                "timed out waiting for the condition\n"
            )
            sys.exit(1)
        del s["releases"][key]
        for c in [c for c in s["controllers"] if c["ns"] == ns and c.get("release") == name]:
            remove_controller(s, c)
        reconcile(s)
        save(s)
        print('release "%s" uninstalled' % name)
    else:
        unhandled("helm", args)
"""

_STUB = (
    "#!{python}\nimport sys\nsys.path.insert(0, {dir!r})\n"
    "import fake_cluster\nfake_cluster.{main}()\n"
)


class Cluster:
    """Builder for the fake cluster state (written to the state dir before the run)."""

    def __init__(self) -> None:
        self.s: dict[str, Any] = {
            "context": None,
            "uid_counter": 0,
            "namespaces": {},
            "ns_cfg": {},
            "ns_reads": {},
            "controllers": [],
            "replicasets": [],
            "pods": [],
            "pvcs": [],
            "pvs": [],
            "secrets": [],
            "releases": {},
            "late_pods": {},
            "fail_get_pods": [],
            "recreations": 0,
            "late_spawned": 0,
        }

    def namespace(
        self,
        ns: str,
        block: str | None = None,
        *,
        vanish_after: int | None = None,
        read_fail_from: int | None = None,
    ) -> None:
        """Add a namespace. `block`: its deletion times out with that termination condition.
        `vanish_after=N`: it disappears (finishes finalizing) on the read after the Nth
        `get namespace -o name`. `read_fail_from=N`: those reads fail from the Nth on."""
        self.s["namespaces"][ns] = {"block": block}
        self.s["ns_cfg"][ns] = {"vanish_after": vanish_after, "read_fail_from": read_fail_from}

    def release(
        self,
        ns: str,
        name: str,
        *,
        uninstall: str = "ok",
        drop_record: bool = False,
        values: dict[str, Any] | None = None,
    ) -> None:
        self.s["releases"][f"{ns}/{name}"] = {
            "uninstall": uninstall,
            "drop_record": drop_record,
            "values": values or {},
        }

    def _uid(self) -> str:
        self.s["uid_counter"] += 1
        return f"uid-{self.s['uid_counter']}"

    def controller(
        self,
        kind: str,
        ns: str,
        name: str,
        labels: dict[str, str],
        *,
        claims: tuple[str, ...] = (),
        stuck: bool = False,
        unremovable: bool = False,
        release: str | None = None,
        pod_labels: dict[str, str] | None = None,
        rs_name: str | None = None,
    ) -> str:
        """Add a controller plus its initial pod; returns that pod's name."""
        c = {
            "kind": kind,
            "ns": ns,
            "name": name,
            "labels": labels,
            "claims": list(claims),
            "stuck": stuck,
            "unremovable": unremovable,
            "release": release,
            "pod_labels": pod_labels,
            "rs_name": rs_name,
            "gen": 0,
        }
        self.s["controllers"].append(c)
        if kind == "Deployment":
            rs = rs_name or f"{name}-5d9f"
            if not any(r["ns"] == ns and r["name"] == rs for r in self.s["replicasets"]):
                self.replicaset(ns, rs, f"Deployment/{name}")
            owners = [["ReplicaSet", rs]]
            pod_name = f"{rs}-p0"
        else:
            owners = [[kind, name]]
            pod_name = f"{name}-0"
        self.pod(
            ns,
            pod_name,
            pod_labels or labels,
            owners,
            claims=claims,
            stuck=stuck,
            unremovable=unremovable,
        )
        return pod_name

    def pod(
        self,
        ns: str,
        name: str,
        labels: dict[str, str],
        owners: list[list[str]],
        *,
        claims: tuple[str, ...] = (),
        stuck: bool = False,
        unremovable: bool = False,
        terminating: bool = False,
    ) -> None:
        self.s["pods"].append(
            {
                "name": name,
                "ns": ns,
                "uid": self._uid(),
                "labels": labels,
                "owners": owners,
                "claims": list(claims),
                "stuck": stuck,
                "unremovable": unremovable,
                "terminating": terminating,
            }
        )

    def replicaset(self, ns: str, name: str, owner: str) -> None:
        self.s["replicasets"].append({"ns": ns, "name": name, "owner": owner})

    def pvc(self, ns: str, name: str, labels: dict[str, str], *, retain: bool = False) -> None:
        pv = f"pv-{name}"
        self.s["pvcs"].append({"ns": ns, "name": name, "labels": labels, "pv": pv})
        self.s["pvs"].append({"name": pv, "retain": retain})

    def secret(self, ns: str, name: str) -> None:
        self.s["secrets"].append({"ns": ns, "name": name})

    def late_pod(self, ns: str, deployment: str, name: str, labels: dict[str, str]) -> None:
        """A pod the Deployment's ReplicaSet creates as the Deployment is deleted."""
        self.s["late_pods"].setdefault(f"{ns}/{deployment}", []).append(
            {"name": name, "labels": labels, "owner_rs": f"{deployment}-late1"}
        )


def _dev_stack(
    c: Cluster,
    *,
    langfuse_release: str = "timeout",
    clickhouse_stuck: bool = True,
    retain_pv: bool = False,
    clickhouse_labelled: bool = False,
    dummy_ns: str = DUMMY_NS,
    langfuse_ns: str = LANGFUSE_NS,
) -> None:
    """A dev stack: dataspoke umbrella, Langfuse (one workload outside the release label)."""
    for ns in (DATAHUB_NS, NS, langfuse_ns, dummy_ns):
        c.namespace(ns)
    c.release(NS, "dataspoke")
    c.release(langfuse_ns, "langfuse", uninstall=langfuse_release)
    inst = {"app.kubernetes.io/instance": "dataspoke"}
    c.controller(
        "StatefulSet",
        NS,
        "dataspoke-postgresql",
        inst,
        claims=(POSTGRES_PVC,),
        release="dataspoke",
    )
    c.controller(
        "Deployment",
        NS,
        "dataspoke-airflow-api-server",
        {"release": "dataspoke", "tier": "airflow"},
        release="dataspoke",
    )
    c.controller(
        "Deployment",
        NS,
        "dataspoke-api",
        {"app.kubernetes.io/name": "dataspoke-api"},
        release="dataspoke",
        rs_name="dataspoke-api-5d9f",
    )
    c.pvc(NS, POSTGRES_PVC, inst, retain=retain_pv)
    lf = {"app.kubernetes.io/instance": "langfuse"}
    c.controller(
        "StatefulSet",
        langfuse_ns,
        "langfuse-postgresql",
        lf,
        claims=("data-langfuse-postgresql-0",),
        release="langfuse",
    )
    c.controller("Deployment", langfuse_ns, "langfuse-web", lf, release="langfuse")
    # No instance label: only the widened sweep can reach it (the reporter's case).
    c.controller(
        "StatefulSet",
        langfuse_ns,
        "langfuse-clickhouse",
        lf if clickhouse_labelled else {"app": "clickhouse"},
        claims=("data-langfuse-clickhouse-0",),
        stuck=clickhouse_stuck,
        release="langfuse",
    )
    c.pvc(langfuse_ns, "data-langfuse-postgresql-0", lf)
    c.pvc(langfuse_ns, "data-langfuse-clickhouse-0", lf)


def _prod_stack(c: Cluster, *, helm: str = "timeout", postgres_stuck: bool = False) -> None:
    c.namespace(NS)
    c.release(
        NS, "dataspoke", uninstall=helm, values={"secrets": {"existingSecret": "operator-creds"}}
    )
    inst = {"app.kubernetes.io/instance": "dataspoke"}
    c.controller(
        "StatefulSet",
        NS,
        "dataspoke-postgresql",
        inst,
        claims=(POSTGRES_PVC,),
        stuck=postgres_stuck,
        release="dataspoke",
    )
    c.controller(
        "StatefulSet",
        NS,
        "dataspoke-redis-master",
        inst,
        claims=("redis-data-dataspoke-redis-master-0",),
        release="dataspoke",
    )
    c.controller(
        "Deployment",
        NS,
        "dataspoke-airflow-api-server",
        {"release": "dataspoke", "tier": "airflow"},
        release="dataspoke",
    )
    c.controller(
        "Deployment",
        NS,
        "dataspoke-api",
        {"app.kubernetes.io/name": "dataspoke-api"},
        release="dataspoke",
        rs_name="dataspoke-api-5d9f",
    )
    c.pvc(NS, POSTGRES_PVC, inst)
    c.pvc(NS, "redis-data-dataspoke-redis-master-0", inst)
    for name in (
        "operator-creds",
        "dataspoke-airflow-metadata-db",
        "dataspoke-airflow-api-secret-key",
        "dataspoke-airflow-jwt-secret",
        "dataspoke-airflow-metadata-encryption-key",
    ):
        c.secret(NS, name)


# --------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------


@dataclass
class Result:
    rc: int
    out: str
    elapsed: float
    calls: list[dict[str, Any]]
    state: dict[str, Any]
    unhandled: str

    def kubectl(self) -> list[list[str]]:
        return [c["args"] for c in self.calls if c["tool"] == "kubectl"]

    def helm(self) -> list[list[str]]:
        return [c["args"] for c in self.calls if c["tool"] == "helm"]

    def index(self, pred: Any, *, last: bool = False) -> int:
        """Position in the full call log of the first (or last) call satisfying pred."""
        hits = [i for i, c in enumerate(self.calls) if pred(c)]
        assert hits, "no call matched"
        return hits[-1] if last else hits[0]


def _is_kubectl(call: dict[str, Any], *prefix: str) -> bool:
    return call["tool"] == "kubectl" and call["args"][: len(prefix)] == list(prefix)


def _ns_of(args: list[str]) -> str | None:
    return args[args.index("-n") + 1] if "-n" in args else None


def _is_controller_delete(call: dict[str, Any], ns: str) -> bool:
    a = call["args"]
    return (
        call["tool"] == "kubectl"
        and a[:1] == ["delete"]
        and len(a) > 1
        and (a[1].startswith("statefulset") or a[1] == "deployment")
        and _ns_of(a) == ns
    )


def _is_force_pod_delete(call: dict[str, Any], ns: str | None = None) -> bool:
    a = call["args"]
    return (
        call["tool"] == "kubectl"
        and a[:2] == ["delete", "pod"]
        and "--force" in a
        and (ns is None or _ns_of(a) == ns)
    )


def _pod_delete_names(res: Result, *, force_only: bool) -> set[str]:
    names: set[str] = set()
    for args in res.kubectl():
        if args[:2] != ["delete", "pod"]:
            continue
        if force_only and "--force" not in args and "--grace-period=0" not in args:
            continue
        names.update(a for a in args[2:] if not a.startswith("-") and a not in {_ns_of(args)})
    return names


def _force_deleted_names(res: Result) -> set[str]:
    """Pods named by a `delete pod` call carrying --force / --grace-period=0 (positive checks)."""
    return _pod_delete_names(res, force_only=True)


def _deleted_pod_names(res: Result) -> set[str]:
    """Pods named by ANY `delete pod` call (for "never deleted" checks)."""
    return _pod_delete_names(res, force_only=False)


def run_uninstall(
    tmp_path: Path,
    cluster: Cluster,
    profile: str,
    *flags: str,
    env: dict[str, str] | None = None,
    delete_secs: int | str | None = DELETE_SECS,
    release_secs: int | str | None = RELEASE_SECS,
    langfuse_ns: str = LANGFUSE_NS,
    dummy_ns: str = DUMMY_NS,
    ingress_mode: str = "shared",
) -> Result:
    fake = tmp_path / "fake"
    fake.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (fake / "fake_cluster.py").write_text(_FAKE_MODULE)
    (fake / "state.json").write_text(json.dumps(cluster.s))
    for tool, main in (("kubectl", "kubectl_main"), ("helm", "helm_main")):
        stub = bindir / tool
        stub.write_text(_STUB.format(python=sys.executable, dir=str(fake), main=main))
        stub.chmod(0o755)

    env_file = tmp_path / f".env.{profile}"
    lines = [f"DATASPOKE_KUBE_CLUSTER={CLUSTER}", f"DATASPOKE_KUBE_DATASPOKE_NAMESPACE={NS}"]
    lines.append(f"DATASPOKE_KUBE_INGRESS_MODE={ingress_mode}")
    if profile == "dev":
        lines += [
            f"DATASPOKE_DEV_KUBE_DATAHUB_NAMESPACE={DATAHUB_NS}",
            f"DATASPOKE_DEV_KUBE_LANGFUSE_NAMESPACE={langfuse_ns}",
            f"DATASPOKE_DEV_KUBE_DUMMY_DATA_NAMESPACE={dummy_ns}",
        ]
    env_file.write_text("\n".join(lines) + "\n")

    scratch = tmp_path / "tmp"
    scratch.mkdir()
    full_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DATASPOKE_", "KUBECONFIG")) and k != "ENV_FILE"
    }
    full_env.update(
        PATH=f"{bindir}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        FAKE_CLUSTER_DIR=str(fake),
        TMPDIR=str(scratch),
    )
    if delete_secs is not None:
        full_env["DATASPOKE_UNINSTALL_DELETE_TIMEOUT_SECS"] = str(delete_secs)
    if release_secs is not None:
        full_env["DATASPOKE_UNINSTALL_RELEASE_TIMEOUT_SECS"] = str(release_secs)
    full_env.update(env or {})

    started = time.monotonic()
    proc = subprocess.Popen(
        [
            BASH,
            str(SCRIPT),
            "--profile",
            profile,
            "--env-file",
            str(env_file),
            "--no-question",
            *flags,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=full_env,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=HARD_LIMIT_SECS)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        pytest.fail(
            f"uninstall.sh did not finish within {HARD_LIMIT_SECS}s (an unbounded wait?):\n{out}"
        )
    elapsed = time.monotonic() - started
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # reap anything a stub left behind
    except ProcessLookupError:
        pass

    log = fake / "calls.log"
    calls = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
    unhandled = (fake / "unhandled.log").read_text() if (fake / "unhandled.log").exists() else ""
    return Result(
        rc=proc.returncode,
        out=out,
        elapsed=elapsed,
        calls=calls,
        state=json.loads((fake / "state.json").read_text()),
        unhandled=unhandled,
    )


def _default_warnings(out: str, *only: str) -> list[str]:
    """Output lines naming a timeout env var and mentioning "default" (spec-level matcher)."""
    names = only or (
        "DATASPOKE_UNINSTALL_DELETE_TIMEOUT_SECS",
        "DATASPOKE_UNINSTALL_RELEASE_TIMEOUT_SECS",
    )
    return [
        ln for ln in out.splitlines() if any(n in ln for n in names) and "default" in ln.lower()
    ]


def _names(state: dict[str, Any], key: str, ns: str | None = None) -> set[str]:
    return {o["name"] for o in state[key] if ns is None or o["ns"] == ns}


def _assert_clean_run(res: Result) -> None:
    assert not res.unhandled, f"script issued a call the fakes do not model:\n{res.unhandled}"


# --------------------------------------------------------------------------------------
# Dev profile: Langfuse orphan scenario (the issue)
# --------------------------------------------------------------------------------------


def test_langfuse_helm_timeout_controllers_deleted_before_force_and_nothing_survives(
    tmp_path: Path,
) -> None:
    """The reported bug. Spec §Bounded teardown: "Whatever Helm's outcome, the script then
    deletes the release's controllers ... Deleting only pods cannot work: a surviving controller
    recreates them. Only when pods outlive the wait is a force-delete ... issued, which is
    effective because their controllers are already gone." Spec §Uninstallation (--delete-all):
    nothing of the stack is left, and the Helm failure is a warning carrying Helm's error."""
    cluster = Cluster()
    _dev_stack(cluster)
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all")
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    # Backstops: the helm failure and a stuck pod really occurred, so the ordering below is
    # not vacuous.
    assert any(a[:2] == ["uninstall", "langfuse"] for a in res.helm())
    assert "timed out waiting for the condition" in res.out, "warning must carry Helm's own error"
    force_calls = [c for c in res.calls if _is_force_pod_delete(c, LANGFUSE_NS)]
    assert force_calls, "the stuck Langfuse pod should have needed a force delete"

    first_force = res.index(lambda c: _is_force_pod_delete(c, LANGFUSE_NS))
    last_controller = res.index(lambda c: _is_controller_delete(c, LANGFUSE_NS), last=True)
    assert last_controller < first_force, "a pod was force-deleted before every controller was gone"
    # The model's own witness: no controller ever recreated a pod during the whole run.
    assert res.state["recreations"] == 0

    s = res.state
    assert not s["controllers"] and not s["pods"] and not s["pvcs"], "workload or PVC survived"
    assert not s["pvs"], "a PV survived its claim's deletion"
    assert not s["namespaces"], f"namespaces survived: {sorted(s['namespaces'])}"


def test_widened_sweep_runs_only_after_label_selected_deletion(tmp_path: Path) -> None:
    """Spec §Bounded teardown (sweep scope table): dev Langfuse sweeps "the
    `app.kubernetes.io/instance=langfuse` label first; a widened sweep of every workload in the
    Langfuse namespace only under the guard". The unlabelled ClickHouse workload still goes."""
    cluster = Cluster()
    _dev_stack(cluster, clickhouse_stuck=False)
    res = run_uninstall(tmp_path, cluster, "dev")
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    lf_sweeps = [
        a
        for a in res.kubectl()
        if a[:1] == ["delete"] and _ns_of(a) == LANGFUSE_NS and "--all" in a
    ]
    assert lf_sweeps, "widened sweep (--all) was needed for the unlabelled workload and never ran"
    labelled = res.index(
        lambda c: (
            _is_controller_delete(c, LANGFUSE_NS)
            and "app.kubernetes.io/instance=langfuse" in c["args"]
        )
    )
    wide = res.index(lambda c: _is_controller_delete(c, LANGFUSE_NS) and "--all" in c["args"])
    assert labelled < wide
    assert not _names(res.state, "controllers", LANGFUSE_NS)


def test_widened_sweep_not_run_when_label_selected_deletion_leaves_nothing(
    tmp_path: Path,
) -> None:
    """Spec §Bounded teardown: the widened Langfuse sweep "runs only when label-selected
    deletion leaves workloads" in the namespace. Here every Langfuse workload carries the
    release label, so no `--all` delete may be issued and the namespace ends up empty."""
    cluster = Cluster()
    _dev_stack(cluster, clickhouse_stuck=False, clickhouse_labelled=True)
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all")
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    # Backstop: the label-selected sweep ran in the Langfuse namespace and did the work.
    assert any(
        _is_controller_delete(c, LANGFUSE_NS) and "app.kubernetes.io/instance=langfuse" in c["args"]
        for c in res.calls
    )
    assert [
        a
        for a in res.kubectl()
        if a[:1] == ["delete"] and _ns_of(a) == LANGFUSE_NS and "--all" in a
    ] == []
    assert LANGFUSE_NS not in res.state["namespaces"]
    assert not _names(res.state, "controllers", LANGFUSE_NS)
    assert not _names(res.state, "pods", LANGFUSE_NS)


def test_widened_sweep_blocked_by_namespace_guard(tmp_path: Path) -> None:
    """Spec §Bounded teardown: "A namespace failing the guard never gets the widened sweep; its
    leftover workloads are recorded as unresolved." `kube-public` is on the guard's list."""
    cluster = Cluster()
    for ns in (DATAHUB_NS, NS, "kube-public", DUMMY_NS):
        cluster.namespace(ns)
    cluster.controller("StatefulSet", "kube-public", "shared-thing", {"app": "something-else"})
    res = run_uninstall(tmp_path, cluster, "dev", langfuse_ns="kube-public")
    _assert_clean_run(res)

    assert [a for a in res.kubectl() if a[:1] == ["delete"] and "--all" in a] == []
    assert _names(res.state, "controllers", "kube-public") == {"shared-thing"}
    assert res.rc != 0
    assert "shared-thing" in res.out and "kube-public" in res.out


# --------------------------------------------------------------------------------------
# Time-bounded deletions
# --------------------------------------------------------------------------------------


def test_every_pvc_and_namespace_delete_carries_timeout(tmp_path: Path) -> None:
    """Spec §Bounded teardown: "Every PVC and namespace deletion waits at most the delete
    timeout"; the env var bounds each PVC and namespace deletion. The value must be the
    configured one (DATASPOKE_UNINSTALL_DELETE_TIMEOUT_SECS), not a hard-coded guess."""
    cluster = Cluster()
    _dev_stack(cluster)
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all")
    _assert_clean_run(res)
    assert res.rc == 0, res.out

    pvc_deletes = [
        a for a in res.kubectl() if a[:1] == ["delete"] and a[1] in ("pvc", "persistentvolumeclaim")
    ]
    ns_deletes = [a for a in res.kubectl() if a[:1] == ["delete"] and a[1] in ("namespace", "ns")]
    # Backstop: all three claims and all four namespaces were actually deleted through kubectl.
    assert {a[2] for a in pvc_deletes} == {
        POSTGRES_PVC,
        "data-langfuse-postgresql-0",
        "data-langfuse-clickhouse-0",
    }
    assert {a[2] for a in ns_deletes} == {DATAHUB_NS, NS, LANGFUSE_NS, DUMMY_NS}
    for args in pvc_deletes + ns_deletes:
        assert f"--timeout={DELETE_SECS}s" in args, f"untimed delete: {args}"


def _code_lines(text: str) -> list[str]:
    """Logical code lines: backslash continuations joined, comments and echoed hints dropped."""
    joined = re.sub(r"\\\n\s*", " ", text)
    out = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"(info|warn|echo|error|printf)\b", line):
            continue  # an operator-facing hint that merely quotes a kubectl command
        out.append(line)
    return out


def test_script_has_no_untimed_pvc_or_namespace_delete() -> None:
    """Static companion to the call-log test: no `kubectl delete pvc|namespace` in the script
    text without `--timeout` (spec §Bounded teardown: those deletions are time-bounded), so a
    branch the fakes do not drive cannot reintroduce an unbounded wait."""
    pattern = re.compile(r"kubectl\s+delete\s+(pvc|persistentvolumeclaims?|namespaces?|ns)\b")
    found = [ln for ln in _code_lines(SCRIPT.read_text()) if pattern.search(ln)]
    assert len(found) >= 2, "expected the PVC and namespace delete sites to be found"
    for line in found:
        assert "--timeout" in line, f"untimed delete in uninstall.sh: {line}"


def test_pvc_held_by_unremovable_pod_ends_bounded_and_names_pvc_and_pod(tmp_path: Path) -> None:
    """Spec §Bounded teardown: on a PVC timeout "the script does not block: it names the PVC
    (with the pods still mounting it and its finalizers - typically
    `kubernetes.io/pvc-protection` ...), records it as unresolved, and continues"; a non-empty
    unresolved set exits non-zero after a closing summary."""
    cluster = Cluster()
    _dev_stack(cluster)
    # Replace the postgres pod's controller state: its pod can never be removed.
    for c in cluster.s["controllers"]:
        if c["name"] == "dataspoke-postgresql":
            c["unremovable"] = True
    for p in cluster.s["pods"]:
        if p["name"] == POSTGRES_POD:
            p["unremovable"] = True
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all")
    _assert_clean_run(res)

    assert res.rc != 0
    # Many bounded steps of DELETE_SECS each; a hang would hit HARD_LIMIT_SECS instead.
    assert res.elapsed < HARD_LIMIT_SECS / 2, f"took {res.elapsed:.0f}s"
    assert POSTGRES_PVC in res.out
    assert POSTGRES_POD in res.out
    assert "kubernetes.io/pvc-protection" in res.out
    assert POSTGRES_PVC in _names(res.state, "pvcs"), "the held claim cannot have been deleted"
    # Everything not held was still removed: the run continued past the stuck claim.
    assert not _names(res.state, "pvcs", LANGFUSE_NS)
    assert LANGFUSE_NS not in res.state["namespaces"]
    assert any(
        a[:2] == ["delete", "pvc"] and a[2] == POSTGRES_PVC and f"--timeout={DELETE_SECS}s" in a
        for a in res.kubectl()
    )


def test_namespace_stuck_terminating_is_named_with_conditions_and_run_continues(
    tmp_path: Path,
) -> None:
    """Spec §Bounded teardown: a namespace that times out is named "with its termination
    conditions", recorded as unresolved, and the script continues (other namespaces still go)."""
    cluster = Cluster()
    _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
    cluster.namespace(LANGFUSE_NS, block="NamespaceContentRemaining: some content is remaining")
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all")
    _assert_clean_run(res)

    assert res.rc != 0
    assert LANGFUSE_NS in res.out
    assert "NamespaceContentRemaining" in res.out
    assert set(res.state["namespaces"]) == {LANGFUSE_NS}, "only the stuck namespace may remain"
    ns_calls = [
        a for a in res.kubectl() if a[:2] == ["delete", "namespace"] and a[2] == LANGFUSE_NS
    ]
    assert ns_calls and f"--timeout={DELETE_SECS}s" in ns_calls[0]


def test_retained_pv_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    """Spec §Bounded teardown: a PV that remains after the bounded wait "is reported as
    'retained or still releasing' rather than a failure, because a `Retain` StorageClass
    legitimately leaves it" and "is only a warning and is not part of the set"."""
    cluster = Cluster()
    _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False, retain_pv=True)
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all")
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    assert "retained or still releasing" in res.out
    assert f"pv-{POSTGRES_PVC}" in res.out
    assert {v["name"] for v in res.state["pvs"]} == {f"pv-{POSTGRES_PVC}"}


# --------------------------------------------------------------------------------------
# Scope of the controller sweep
# --------------------------------------------------------------------------------------


def test_dev_umbrella_sweep_stays_within_the_three_selectors(tmp_path: Path) -> None:
    """Spec §Bounded teardown (selector table): "no selector reaches beyond" the three
    umbrella selectors - the instance label, `release=dataspoke,tier=airflow`, and the
    Deployment `dataspoke-api` by exact object name. `--all` is only for the guarded Langfuse
    namespace. Objects outside the three (an unrelated workload, an object copying the
    API's app-identity label) survive."""
    cluster = Cluster()
    _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
    cluster.controller("Deployment", NS, "unrelated", {"app": "unrelated"})
    cluster.controller(
        "Deployment",
        NS,
        "operator-api",
        {"app.kubernetes.io/name": "dataspoke-api"},
        rs_name="operator-api-abc12",
    )
    res = run_uninstall(tmp_path, cluster, "dev")
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    assert _names(res.state, "controllers", NS) == {"unrelated", "operator-api"}
    sweeps = [
        a for a in res.kubectl() if a[:1] == ["delete"] and _ns_of(a) == NS and a[1] != "secret"
    ]
    assert sweeps, "the umbrella sweep never ran"
    for args in sweeps:
        assert "--all" not in args, f"umbrella sweep used --all: {args}"
        if args[1] == "deployment":
            assert args[2] == "dataspoke-api", (
                f"deployment deleted by something other than exact name: {args}"
            )
        else:
            assert args[args.index("-l") + 1] in UMBRELLA_LABELS, args
    # Backstop: the in-scope workloads were removed.
    assert not (_names(res.state, "controllers", NS) & {"dataspoke-postgresql", "dataspoke-api"})


# --------------------------------------------------------------------------------------
# dataspoke-api pod scope (snapshot before the sweep)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["dev", "prod"])
def test_owned_api_pod_stuck_terminating_is_force_deleted_after_replicaset_gc(
    tmp_path: Path, profile: str
) -> None:
    """Spec §Bounded teardown: the API pods are "matched by that label and must additionally be
    owned by a ReplicaSet of that Deployment, which excludes look-alike Deployments", and
    pods that outlive the wait are force-deleted. Deleting the Deployment garbage-collects the
    ReplicaSet, so ownership must still be recognised for the stuck pod afterwards. A
    look-alike (a ReplicaSet controlled by another Deployment) is never force-deleted."""
    cluster = Cluster()
    if profile == "dev":
        for ns in (DATAHUB_NS, NS, LANGFUSE_NS, DUMMY_NS):
            cluster.namespace(ns)
    else:
        cluster.namespace(NS)
    owned = cluster.controller(
        "Deployment",
        NS,
        "dataspoke-api",
        {"app.kubernetes.io/name": "dataspoke-api"},
        stuck=True,
        rs_name="dataspoke-api-5d9f",
    )
    # Same label, ReplicaSet controlled by another Deployment; terminating and tempting.
    cluster.replicaset(NS, "dataspoke-api-zz9", "Deployment/other-api")
    cluster.pod(
        NS,
        "lookalike-pod",
        {"app.kubernetes.io/name": "dataspoke-api"},
        [["ReplicaSet", "dataspoke-api-zz9"]],
        stuck=True,
        terminating=True,
    )
    res = run_uninstall(tmp_path, cluster, profile)
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    assert owned in _force_deleted_names(res), "the owned stuck API pod was never force-deleted"
    assert owned not in _names(res.state, "pods")
    assert "lookalike-pod" not in _deleted_pod_names(res), "a look-alike pod was deleted"
    assert "lookalike-pod" in _names(res.state, "pods"), "the look-alike pod must be left alone"
    assert res.state["recreations"] == 0


@pytest.mark.parametrize("profile", ["dev", "prod"])
def test_api_pod_appearing_after_the_snapshot_is_never_force_deleted_and_is_unverified(
    tmp_path: Path, profile: str
) -> None:
    """Spec §Bounded teardown: a pod that cannot be attributed to the Deployment is not
    force-deleted, and a pod list that cannot be read as complete "is itself recorded as
    unresolved ('could not be listed ... removal could not be verified')", so the run exits
    non-zero rather than reading the leftover as gone. Here a labelled pod stuck terminating
    appears only as the Deployment is deleted (after the pre-sweep snapshot)."""
    cluster = Cluster()
    if profile == "dev":
        for ns in (DATAHUB_NS, NS, LANGFUSE_NS, DUMMY_NS):
            cluster.namespace(ns)
    else:
        cluster.namespace(NS)
    cluster.controller(
        "Deployment",
        NS,
        "dataspoke-api",
        {"app.kubernetes.io/name": "dataspoke-api"},
        rs_name="dataspoke-api-5d9f",
    )
    cluster.late_pod(
        NS, "dataspoke-api", "late-api-pod", {"app.kubernetes.io/name": "dataspoke-api"}
    )
    res = run_uninstall(tmp_path, cluster, profile)
    _assert_clean_run(res)

    assert res.state["late_spawned"] == 1, "the late pod never appeared; the test is vacuous"
    assert "late-api-pod" not in _deleted_pod_names(res)
    assert "late-api-pod" in _names(res.state, "pods"), "the unverified pod must be left alone"
    assert res.rc != 0
    assert "could not be listed" in res.out
    assert "removal could not be verified" in res.out


def test_pod_list_that_cannot_be_read_is_unresolved_not_empty(tmp_path: Path) -> None:
    """Spec §Bounded teardown: "A pod, workload or PVC list that cannot be read is itself
    recorded as unresolved ... an unreadable list is unknown, never read as empty"."""
    cluster = Cluster()
    _prod_stack(cluster)
    cluster.s["fail_get_pods"] = [NS]
    res = run_uninstall(tmp_path, cluster, "prod")
    _assert_clean_run(res)

    assert res.rc != 0
    assert "could not be listed" in res.out
    assert "could not be verified" in res.out
    assert res.elapsed < HARD_LIMIT_SECS / 2


def test_unresolved_entries_in_a_namespace_that_is_then_deleted_are_dropped(
    tmp_path: Path,
) -> None:
    """Spec §Bounded teardown: "Entries scoped to a namespace that a later step deletes
    successfully are dropped, because the pods, claims and workloads in it went with the
    namespace." The Langfuse pod list cannot be read (an unresolved entry); with the
    namespace retained that fails the run (backstop), with `--delete-all` the namespace goes
    and the run succeeds with no such entry in the summary."""

    def langfuse_cluster() -> Cluster:
        cluster = Cluster()
        _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
        cluster.s["fail_get_pods"] = [LANGFUSE_NS]
        return cluster

    (tmp_path / "kept").mkdir()
    kept = run_uninstall(tmp_path / "kept", langfuse_cluster(), "dev")
    _assert_clean_run(kept)
    assert kept.rc != 0
    assert "could not be listed" in kept.out and LANGFUSE_NS in kept.out

    (tmp_path / "deleted").mkdir()
    res = run_uninstall(tmp_path / "deleted", langfuse_cluster(), "dev", "--delete-all")
    _assert_clean_run(res)
    assert LANGFUSE_NS not in res.state["namespaces"], "the namespace was supposed to be deleted"
    assert res.rc == 0, res.out
    assert "could not be listed" not in res.out
    assert "unresolved" not in res.out.lower()


def _summary(out: str) -> str:
    """The closing "unresolved item(s)" summary (empty when the run reported none)."""
    return out.split("unresolved item(s)", 1)[1] if "unresolved item(s)" in out else ""


def _ns_reads(res: Result, ns: str) -> list[dict[str, Any]]:
    """Calls to the bounded `get namespace <ns> --ignore-not-found -o name` read."""
    return [
        c
        for c in res.calls
        if _is_kubectl(c, "get", "namespace", ns) and "-o" in c["args"] and "name" in c["args"]
    ]


def _ns_delete_calls(res: Result, ns: str) -> list[dict[str, Any]]:
    return [c for c in res.calls if _is_kubectl(c, "delete", "namespace", ns)]


STUCK_MSG = "NamespaceContentRemaining: some content is remaining"


def test_timed_out_namespace_gone_at_recheck_clears_its_entries(tmp_path: Path) -> None:
    """Spec §Bounded teardown: a namespace whose deletion timed out "is checked once more
    before the closing summary"; "if it is gone, its entry is dropped from the unresolved set,
    along with every entry scoped to it", so the run can exit 0. Here the Langfuse namespace
    finishes finalizing after the delete times out, and its pod list was unreadable."""
    cluster = Cluster()
    _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
    cluster.namespace(LANGFUSE_NS, block=STUCK_MSG, vanish_after=1)
    cluster.s["fail_get_pods"] = [LANGFUSE_NS]
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all")
    _assert_clean_run(res)

    # Backstops: the deletion really timed out and the re-check really read the namespace.
    assert f"Namespace '{LANGFUSE_NS}' not deleted within" in res.out
    assert len(_ns_reads(res, LANGFUSE_NS)) >= 2
    assert LANGFUSE_NS not in res.state["namespaces"]
    assert res.rc == 0, res.out
    assert f"namespace {LANGFUSE_NS} not deleted" not in res.out
    assert "could not be listed" not in res.out
    assert _summary(res.out) == "", "no unresolved summary expected"


def test_timed_out_namespace_still_present_at_recheck_is_kept_and_wait_is_bounded(
    tmp_path: Path,
) -> None:
    """Spec §Bounded teardown: a namespace "still exists" at the re-check keeps its entry and
    scoped entries (non-zero exit), and its deletion "waits at most two delete-timeout windows
    in total" - the delete itself, then at most one more window of re-check reads."""
    cluster = Cluster()
    _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
    cluster.namespace(LANGFUSE_NS, block=STUCK_MSG)
    cluster.s["fail_get_pods"] = [LANGFUSE_NS]
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all")
    _assert_clean_run(res)

    assert res.rc != 0
    summary = _summary(res.out)
    assert f"namespace {LANGFUSE_NS} not deleted" in summary
    assert "could not be listed" in summary and f"pods in {LANGFUSE_NS}" in summary

    deletes = _ns_delete_calls(res, LANGFUSE_NS)
    assert len(deletes) == 1, "a timed-out namespace is not deleted a second time"
    started = deletes[0]["t"]
    rechecks = [c["t"] for c in _ns_reads(res, LANGFUSE_NS) if c["t"] > started]
    assert len(rechecks) >= 2, "the re-check should poll within its window"
    # Two windows in total: the delete (DELETE_SECS) plus at most one more; slack covers
    # process start-up and the script's 1s poll granularity, not a third window.
    assert max(rechecks) - started <= 2 * DELETE_SECS + 3, max(rechecks) - started
    assert max(rechecks) - started >= DELETE_SECS


def test_unreadable_recheck_keeps_the_namespace_unresolved(tmp_path: Path) -> None:
    """Spec §Bounded teardown: "a namespace counts as gone only when a successful bounded read
    reports it not found"; a failed read "is unknown, never read as ... gone". The namespace
    does vanish here, but every re-check read fails, so the entries stay."""
    cluster = Cluster()
    _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
    cluster.namespace(LANGFUSE_NS, block=STUCK_MSG, vanish_after=1, read_fail_from=2)
    cluster.s["fail_get_pods"] = [LANGFUSE_NS]
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all")
    _assert_clean_run(res)

    assert len(_ns_reads(res, LANGFUSE_NS)) >= 2, "the re-check never ran (vacuous test)"
    assert res.rc != 0
    summary = _summary(res.out)
    assert f"namespace {LANGFUSE_NS} not deleted" in summary
    assert f"pods in {LANGFUSE_NS}" in summary


@pytest.mark.parametrize("site", ["ingress-nginx", "dev", "prod"])
def test_unreadable_namespace_presence_is_unresolved_and_deletion_not_attempted(
    tmp_path: Path, site: str
) -> None:
    """Spec §Bounded teardown: the presence check before each bounded namespace deletion skips a
    namespace "as absent only on a successful not-found read"; a read that fails "records the
    namespace as unresolved ('could not be read - deletion not attempted') and the run exits
    non-zero". Covers the three call sites: ingress-nginx (managed mode), a dev namespace and
    the prod namespace."""
    cluster = Cluster()
    flags: tuple[str, ...]
    if site == "ingress-nginx":
        target, profile, flags = "ingress-nginx", "dev", ()
        _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
        cluster.namespace(target, read_fail_from=1)
        res = run_uninstall(tmp_path, cluster, profile, ingress_mode="managed")
    elif site == "dev":
        target, profile, flags = DUMMY_NS, "dev", ("--delete-all",)
        _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
        cluster.namespace(target, read_fail_from=1)
        res = run_uninstall(tmp_path, cluster, profile, *flags)
    else:
        target, profile, flags = NS, "prod", ("--delete-namespaces",)
        _prod_stack(cluster, helm="ok")
        cluster.namespace(target, read_fail_from=1)
        res = run_uninstall(tmp_path, cluster, profile, *flags)
    _assert_clean_run(res)

    assert _ns_reads(res, target), "the presence read never ran (vacuous test)"
    assert _ns_delete_calls(res, target) == [], "deleted a namespace it could not read"
    assert target in res.state["namespaces"]
    assert res.rc != 0
    summary = _summary(res.out)
    assert f"namespace {target} could not be read" in summary
    assert "deletion not attempted" in summary
    if site == "dev":  # the run continues: the readable namespaces are still deleted
        assert set(res.state["namespaces"]) == {DUMMY_NS}


def test_namespace_presence_read_is_bounded_by_the_delete_timeout(tmp_path: Path) -> None:
    """Spec §Bounded teardown (env var table): DATASPOKE_UNINSTALL_DELETE_TIMEOUT_SECS bounds
    the "namespace presence check before deletion", which is a `--ignore-not-found` read
    (empty success = absent). The first read of each namespace carries that bound as its
    request timeout and precedes the delete."""
    cluster = Cluster()
    _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all", delete_secs=7)
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    for ns in (DATAHUB_NS, NS, LANGFUSE_NS, DUMMY_NS):
        reads = _ns_reads(res, ns)
        assert reads, f"no presence read for {ns}"
        assert "--request-timeout=7s" in reads[0]["args"]
        assert "--ignore-not-found" in reads[0]["args"]
        deletes = _ns_delete_calls(res, ns)
        assert deletes and reads[0]["t"] <= deletes[0]["t"]
        assert res.calls.index(reads[0]) < res.calls.index(deletes[0])


@pytest.mark.parametrize("via", ["delete", "recheck"])
@pytest.mark.parametrize(("lf_ns", "gone_ns"), [("lf-t", "lf"), ("lf10", "lf1")])
def test_entries_of_a_similarly_named_namespace_are_not_dropped(
    tmp_path: Path, lf_ns: str, gone_ns: str, via: str
) -> None:
    """Spec §Bounded teardown: only "entries scoped to" a namespace whose deletion is confirmed
    are dropped. `gone_ns` is deleted (cleanly, or after a timed-out delete that the re-check
    confirms) while `lf_ns` - whose name merely extends it - stays stuck: its own entries must
    survive in the summary."""
    cluster = Cluster()
    _dev_stack(
        cluster,
        langfuse_release="ok",
        clickhouse_stuck=False,
        langfuse_ns=lf_ns,
        dummy_ns=gone_ns,
    )
    cluster.namespace(lf_ns, block=STUCK_MSG)
    if via == "recheck":
        cluster.namespace(gone_ns, block=STUCK_MSG, vanish_after=1)
    cluster.s["fail_get_pods"] = [lf_ns]
    res = run_uninstall(
        tmp_path, cluster, "dev", "--delete-all", langfuse_ns=lf_ns, dummy_ns=gone_ns
    )
    _assert_clean_run(res)

    assert gone_ns not in res.state["namespaces"], "the shorter-named namespace should be gone"
    assert lf_ns in res.state["namespaces"]
    assert res.rc != 0
    summary = _summary(res.out)
    assert f"namespace {lf_ns} not deleted" in summary
    assert f"pods in {lf_ns} could not be listed" in summary
    assert f"namespace {gone_ns} " not in summary


# --------------------------------------------------------------------------------------
# Prod profile
# --------------------------------------------------------------------------------------


def test_prod_helm_timeout_still_cleans_secrets_and_never_deletes_pvcs_or_uses_all(
    tmp_path: Path,
) -> None:
    """Spec §Bounded teardown: "a timed-out prod umbrella uninstall still reaches the Secret
    cleanup that follows it"; the prod sweep is the three selectors "never every workload - the
    prod namespace may hold operator-owned objects"; "the prod profile never issues a PVC
    deletion" (§What a prod uninstall leaves behind: the three PVCs and the operator Secret
    are retained)."""
    cluster = Cluster()
    _prod_stack(cluster)
    # Operator-owned objects that must survive: one copying the API's app-identity label.
    cluster.controller("Deployment", NS, "operator-metrics", {"team": "ops"})
    cluster.controller(
        "Deployment",
        NS,
        "operator-api",
        {"app.kubernetes.io/name": "dataspoke-api"},
        rs_name="operator-api-abc12",
    )
    res = run_uninstall(tmp_path, cluster, "prod", "--delete-pvcs")
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    assert "timed out waiting for the condition" in res.out, "warning must carry Helm's own error"
    # Secret cleanup ran after the failed Helm uninstall.
    secrets = {x["name"] for x in res.state["secrets"]}
    assert secrets == {"operator-creds"}, f"unexpected secrets left: {secrets}"
    helm_i = res.index(lambda c: c["tool"] == "helm" and c["args"][:1] == ["uninstall"])
    secret_i = res.index(lambda c: _is_kubectl(c, "delete", "secret"))
    assert helm_i < secret_i

    # PVCs retained, never deleted (even though --delete-pvcs was passed: it is dev-only).
    assert [
        a for a in res.kubectl() if a[:1] == ["delete"] and a[1] in ("pvc", "persistentvolumeclaim")
    ] == []
    assert _names(res.state, "pvcs") == {POSTGRES_PVC, "redis-data-dataspoke-redis-master-0"}

    # Sweep scope.
    assert [a for a in res.kubectl() if "--all" in a] == []
    assert _names(res.state, "controllers", NS) == {"operator-metrics", "operator-api"}
    sweeps = [a for a in res.kubectl() if a[:1] == ["delete"] and a[1] != "secret"]
    assert sweeps
    for args in sweeps:
        if args[1] == "deployment":
            assert args[2] == "dataspoke-api"
        else:
            assert args[args.index("-l") + 1] in UMBRELLA_LABELS, args
    assert res.state["recreations"] == 0


def test_prod_never_force_deletes_a_pod_that_mounts_a_pvc(tmp_path: Path) -> None:
    """Spec §Bounded teardown: "In the prod profile a pod that mounts a PVC is never
    force-deleted ... Such pods are reported as unresolved" (non-zero exit, named)."""
    cluster = Cluster()
    _prod_stack(cluster, postgres_stuck=True)
    res = run_uninstall(tmp_path, cluster, "prod")
    _assert_clean_run(res)

    assert [a for a in res.kubectl() if _is_force_pod_delete({"tool": "kubectl", "args": a})] == []
    assert POSTGRES_POD not in _deleted_pod_names(res)
    # Backstop: the stuck pod really was still there to be force-deleted (not a vacuous pass).
    assert POSTGRES_POD in _names(res.state, "pods")
    assert res.rc != 0
    assert POSTGRES_POD in res.out
    assert POSTGRES_PVC in res.out
    assert POSTGRES_PVC in _names(res.state, "pvcs")


def test_prod_force_deletes_a_stuck_pod_without_claims(tmp_path: Path) -> None:
    """Counterpart that keeps the prod rule honest: a stuck pod that mounts nothing is still
    force-deleted once its controller is gone (spec §Bounded teardown), so the previous test
    is about the PVC, not about prod suppressing force deletes wholesale."""
    cluster = Cluster()
    _prod_stack(cluster)
    cluster.controller(
        "Deployment",
        NS,
        "dataspoke-airflow-scheduler",
        {"release": "dataspoke", "tier": "airflow"},
        stuck=True,
        release="dataspoke",
    )
    res = run_uninstall(tmp_path, cluster, "prod")
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    assert "dataspoke-airflow-scheduler-5d9f-p0" in _force_deleted_names(res)
    assert not [p for p in res.state["pods"] if "scheduler" in p["name"]]


# --------------------------------------------------------------------------------------
# Idempotent orphan recovery
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["dev", "prod"])
def test_rerun_after_release_record_is_gone_still_sweeps_orphaned_workloads(
    tmp_path: Path, profile: str
) -> None:
    """Spec §Bounded teardown: "Because the sweep runs unconditionally it is idempotent and
    also recovers workloads orphaned by an earlier timed-out run whose release record is already
    gone."""
    cluster = Cluster()
    if profile == "dev":
        _dev_stack(cluster, clickhouse_stuck=False)
    else:
        _prod_stack(cluster)
    cluster.s["releases"] = {}  # the earlier run's Helm uninstall removed the record
    res = run_uninstall(tmp_path, cluster, profile)
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    assert [a for a in res.helm() if a[:1] == ["uninstall"]] == [], "no release exists to uninstall"
    sweeps = [c for c in res.calls if _is_controller_delete(c, NS)]
    assert sweeps, "the controller sweep must run even when Helm finds no release"
    assert not _names(res.state, "controllers", NS)
    assert not _names(res.state, "pods", NS)
    if profile == "dev":
        assert not _names(res.state, "controllers", LANGFUSE_NS)


# --------------------------------------------------------------------------------------
# Timeout env vars
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("var", "default"),
    [
        ("DATASPOKE_UNINSTALL_DELETE_TIMEOUT_SECS", 120),
        ("DATASPOKE_UNINSTALL_RELEASE_TIMEOUT_SECS", 300),
    ],
)
@pytest.mark.parametrize("bad", ["abc", "0", "86401"])
def test_invalid_timeout_env_var_warns_and_uses_default(
    tmp_path: Path, var: str, default: int, bad: str
) -> None:
    """Spec §Bounded teardown (env var table): "a value that is not a positive integer no
    greater than 86400 is warned about and the default is used" (delete 120s, release 300s)."""
    cluster = Cluster()
    _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
    delete_secs: int | str | None = DELETE_SECS
    release_secs: int | str | None = RELEASE_SECS
    if var.endswith("DELETE_TIMEOUT_SECS"):
        delete_secs = None
    else:
        release_secs = None
    res = run_uninstall(
        tmp_path,
        cluster,
        "dev",
        "--delete-all",
        env={var: bad},
        delete_secs=delete_secs,
        release_secs=release_secs,
    )
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    warning = _default_warnings(res.out, var)
    assert warning, f"no warning naming {var} and the default:\n{res.out}"
    if var.endswith("DELETE_TIMEOUT_SECS"):
        pvc = [a for a in res.kubectl() if a[:2] == ["delete", "pvc"]]
        assert pvc and all(f"--timeout={default}s" in a for a in pvc)
    else:
        uninstalls = [a for a in res.helm() if a[:1] == ["uninstall"]]
        assert uninstalls, "no release was uninstalled; the default cannot be observed"
        assert all(a[a.index("--timeout") + 1] == f"{default}s" for a in uninstalls)


def test_valid_timeout_env_vars_are_used_up_to_the_upper_bound(tmp_path: Path) -> None:
    """Spec §Bounded teardown (env var table): DATASPOKE_UNINSTALL_RELEASE_TIMEOUT_SECS bounds
    `helm uninstall --wait` per release and DATASPOKE_UNINSTALL_DELETE_TIMEOUT_SECS the PVC /
    namespace deletions; 86400 is the largest accepted value, so it is not warned about."""
    cluster = Cluster()
    _dev_stack(cluster, langfuse_release="ok", clickhouse_stuck=False)
    res = run_uninstall(tmp_path, cluster, "dev", "--delete-all", delete_secs=7, release_secs=86400)
    _assert_clean_run(res)

    assert res.rc == 0, res.out
    uninstalls = [
        a for a in res.helm() if a[:1] == ["uninstall"] and a[1] in ("dataspoke", "langfuse")
    ]
    assert {a[1] for a in uninstalls} == {"dataspoke", "langfuse"}
    assert all(a[a.index("--timeout") + 1] == "86400s" for a in uninstalls)
    deletes = [a for a in res.kubectl() if a[:2] in (["delete", "pvc"], ["delete", "namespace"])]
    assert deletes and all("--timeout=7s" in a for a in deletes)
    assert _default_warnings(res.out) == [], "a valid value must not be warned about"

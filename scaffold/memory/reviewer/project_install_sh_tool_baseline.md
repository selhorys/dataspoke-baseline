---
name: install-sh-tool-baseline
description: install.sh only require_tools kubectl/helm/python3 — but digest resolution now hard-aborts prod without a vendor CLI (or a local image), so the effective prod tool baseline grew without the README saying so
metadata:
  type: project
---

`helm-charts/bin/install.sh` runs `require_tools kubectl helm python3` and
nothing else. `docker` is needed only on the build path
(`build-image.sh`, empty/AWS `DATASPOKE_KUBE_CLOUD_VENDOR`), which
`--skip-build` skips entirely — and `--skip-build` is the *documented standard
prod flow* (CI builds and pushes; the deploy host only helms).

**Why:** the dedicated `_check_digest_resolution_possible_prod` pre-flight is
gone, but the dependency it guarded is not. `_resolve_digest_or_abort` runs on
every install and `error`s when `resolve_image_digest` cannot reach a digest —
so `--profile prod --skip-build` on a bastion with only kubectl/helm now
aborts. The empty/local vendor is the worst case: installing docker does not
help, because that branch reads the *local daemon's* `RepoDigests`, which a
CI-pushed image never entered. The escape hatch (`--no-digest-pin`) exists but
is documented only in `--help` and `spec/feature/HELM_CHART.md`, not in
`helm-charts/README.md`'s prod walkthrough or its pre-flight rejection table.
Verified 2026-08-01: the GCP branch works end-to-end against the real
`asia-northeast3-docker.pkg.dev/barry-465712/dataspoke` registry.

**How to apply:** when a generator adds a fail-fast tool/credential check to
install.sh, ask which flag combinations previously reached the end without that
tool. Gate the check on the path that actually needs it (`SKIP_BUILD ==
false`), or make the escape hatch discoverable in the operator-facing README —
a hard abort whose recovery flag appears in no operator doc is a support
incident, not a safety net.

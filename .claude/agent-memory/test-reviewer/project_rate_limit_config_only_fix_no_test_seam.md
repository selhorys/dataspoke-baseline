---
name: rate-limit-config-only-fix-no-test-seam
description: Issue #76's XFF fix is deployment-config only; behavioral unit tests cannot detect the chart wiring being deleted, so the suite needs chart-text assertions — how to judge those at the right altitude
metadata:
  type: project
---

The `_get_user_key` single-bucket bug (issue #76) is fixed entirely in
deployment config: `values.yaml config.trustedProxyIps` →
`templates/configmap.yaml FORWARDED_ALLOW_IPS` → envFrom configMapRef →
`uvicorn/config.py:344 os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")` →
`config.py:513 ProxyHeadersMiddleware(trusted_hosts=...)`. No `src/` change.

1. **Unit tests that construct `ProxyHeadersMiddleware(app, trusted_hosts="…")`
   with a literal trust list test uvicorn, not the wiring.** Verified by
   mutation: deleting the `FORWARDED_ALLOW_IPS:` line from
   `templates/configmap.yaml` once left the whole
   `tests/unit/api/middleware/test_rate_limit.py` suite green. Demand a
   chart-text assertion for any claim that a config-only fix is covered.
   That suite now has three (values default, ConfigMap binding, api-container
   envFrom); the remaining unasserted chart artifact is the render-time
   `{{ fail }}` guard on empty/`"*"`.
2. **`AUTH.md §Client-IP attribution for rate limiting` describes uvicorn's
   algorithm, not a DataSpoke decision.** Its right-to-left chain-walk sentence
   and the over-broad-trust-list hazard have no corresponding DataSpoke code.
   Tests citing them pass the citation-existence check but are third-party
   impl-pinning under T2 unless labelled as dependency canaries.
3. **Altitude test for chart-text assertions.** Line-anchored regex for a key
   plus a substring check on `.Values.<path>` survives key reordering, quoting
   style and whitespace inside `{{ }}` — good altitude. Raw-text *equality* of a
   template expression across two files (`{{ include "x.fullname" . }}-app-config`
   in configmap.yaml vs api-deployment.yaml) breaks on a one-file cosmetic edit;
   require `re.sub(r"\s+", " ", …)` normalisation. Text partitioning to isolate a
   container must be bounded on **both** ends — `partition("- name: api")[2]`
   runs to EOF and lets a sibling container declared after it satisfy the
   assertion.

**Why:** a generator's mutation table asserted the config revert was caught;
independent mutation showed it was not.
**How to apply:** for any deployment-config fix, run the mutation that deletes
the config artifact itself before accepting a coverage claim, and re-run the
cosmetic-reformat mutations before accepting a chart-text assertion. Related:
[[dead-assert-tuple-ruff-blind]].

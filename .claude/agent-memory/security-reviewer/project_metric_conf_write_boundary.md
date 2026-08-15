---
name: metric-conf-write-boundary
description: metric_conf is unvalidated JSONB with three HTTP writers; the time_window_sec bound now lives in src/shared/metric_conf.py and is invoked at each writer, but it is write-boundary-only so a persisted bad row is self-repeating
metadata:
  type: project
---

`metric_definition.metric_conf` is plain `JSONB` (`src/shared/db/models.py:501`)
with **no CHECK constraint**, and the measurers read it with a bare
`int(metric_conf["time_window_sec"])` — they hold no copy of any bound and
trust the write boundary by contract.

**Three writers, asymmetric defense:**

| writer | pydantic check | service-layer check |
|---|---|---|
| `POST` → `create_metric_config` | yes (`_check_metric_conf_for_type`) | **none** |
| `PUT` → `replace_metric_config` | yes | **none** |
| `PATCH` → `patch_metric_config` | no (`metric_conf: dict \| None`) | yes, on the *merged* dict |

So two of three paths rely entirely on the router's request model. All three are
`require_writer` behind a router-level `require_authenticated`, and
`src/backend/metrics/bootstrap.py` is the only non-HTTP writer (hardcoded
`172800`). **If a non-HTTP writer is ever added — a DAG, an `/internal` route, a
seed script — the whole control disappears with it.**

**Where the rule lives now:** `src/shared/metric_conf.py` —
`MAX_TIME_WINDOW_SEC = 315_360_000` (ten years), `is_valid_time_window_sec()`,
`time_window_sec_error(metric_type)`. Both the schema layer and the service
import it, so the two enforcement points cannot drift. The earlier shape put the
constant in `src/api/schemas/metrics.py` and had `src/backend/` import *up* into
`src/api/` — a BACKEND.md §82 layering violation; `src/shared/` is the sanctioned
bridge (same split as `src/shared/dataset_filter.py`).

**Why the bound matters (measured):** both windowed measurers evaluate
`datetime.now(tz=UTC) - timedelta(seconds=window_sec)` at function entry.
`timedelta` caps at 999 999 999 days, so `1e14` raised
`OverflowError: days=1157407407` — unhandled 500 on the on-demand route, task
failure on the scheduled tier — and because the value is *persisted*, it repeated
on every run. Closed for new writes only; existing rows are not healed.

Verified against `model_validate` **and** `model_validate_json` (the real HTTP
path), all rejected: `MAX+1`, `1e14`, `true`/`false` (bool subclasses int), `0`,
`-1`, `172800.0`, `"172800"`, `10**400`. Accepted: `1 … 315_360_000`.

Things that are **not** problems, verified so the next run need not re-check:
- The `metrics:running:{metric_id}` Redis lock is released in a `finally`.
- Recovery via `PATCH {"metric_conf": {...}}` works: the patch **replaces** the
  whole dict (plain `setattr`, not a key merge), then the merged state is
  re-validated.
- `patch_metric_config` `setattr`s the row *before* validating and raises after,
  but `get_db` is `async with SessionLocal()` — close without commit — so a
  rejected PATCH never persists. No "422 but written" hole.
- The 422 for this field reflects the caller's whole request model back
  ([[api-422-echoes-rejected-input]]); benign — no secret-routed field lives in
  `metric_conf`, and the service-side message interpolates only the
  `Literal`-constrained `metric_type`.

**Residual, unbounded:** `metric_conf` is `dict[str, Any]` with no key allowlist
and no size cap; `title` / `description` have no `max_length`. There is no
request-body-size middleware (`src/api/middleware/` is logging + rate_limit
only), so the only cap is the ingress `proxy-body-size`. Extra keys persist to
JSONB and echo back on every GET/list.

**How to apply:** any diff touching `metric_conf` validation must be checked
against all three writers, not just the one it edits. Adding the service-layer
check to create/replace is now a two-line import away — the layering objection
that once justified skipping it is gone.
Related: [[reviewer-config-is-generator-writable]] (`src/api/schemas/**`, where
half this control lives, is still off the sensitive-path glob list).

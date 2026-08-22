---
name: datahub-unavailable-only-retryable
description: DataHubClient converts only retryable transport failures to DataHubUnavailableError; 401/403 fail-fast and SDK GraphError propagate raw, so `except DataHubUnavailableError` misses the commonest GMS fault
metadata:
  type: project
---

`DataHubClient._with_retry` (`src/shared/datahub/client.py`) raises
`DataHubUnavailableError` **only** after exhausting retries on a `ConnectionError` or a
status in `_RETRYABLE_STATUS_CODES = {429,500,502,503,504}`, or when the circuit breaker is
open. Anything in `_FAIL_FAST_STATUS_CODES = {401,403}` is re-raised **as the SDK's own
exception**, and so is every non-retryable error — notably `GraphError`, which the acryl SDK
raises for an HTTP 200 body carrying an `errors` array *and* for a 401/403 from a rotated PAT.
`src/api/routers/internal/activities.py` (role-drift reconcile docstring) already documents
this and deliberately catches bare `Exception` for that reason.

**Why:** any code that treats `DataHubUnavailableError` as "the GMS plane is broken" — health
reporting, circuit logic, retry classification — silently misses the single most likely
production fault, an expired/rotated DataHub PAT. A `peripheral_health` row driven that way
stays pinned at its last `ok` precisely when it is wrong.

**How to apply:** when reviewing any `except DataHubUnavailableError` that is meant to be a
*fault signal* (as opposed to a retry hint), ask what happens on 401/403 and on `GraphError`.
Method docstrings in `client.py` say "Raises: DataHubUnavailableError on transport failure
after retries" — that phrasing is narrower than it reads and is not a guarantee that all
failures arrive as that type. Related: [[verify-branch-reachability-rationales]].

"""Spec conformance for the shared ``metric_conf`` window bound.

Every other test in the tree reads the ceiling through
``src.shared.metric_conf.MAX_TIME_WINDOW_SEC`` — the right call for readability, and it
keeps the schema layer, the service layer and the tests from drifting apart. But it also
means those tests hold for *any* ceiling: a wrong constant moves the expectation with the
implementation. This file is the one place the literal is written out, so the constant is
pinned to the number the spec names rather than to itself.

Spec sources:
  spec/API.md §Metric (/spoke/governance/metric) — Definition body, ``metric_conf`` row:
    "An integer in `[1, 315360000]` (ten years); out of range, non-integer, or boolean
    returns `422 INVALID_PARAMETER`, on `PATCH` as well, where the merged `metric_conf`
    is what is checked."
  spec/feature/BACKEND.md §Metrics Service — Window bounds:
    "`time_window_sec` is an integer in `[1, 315_360_000]` — one second to ten years."
  spec/feature/FRONTEND_GOVERNANCE.md §Metrics (`/governance/metrics`) — the form's
    window input "constrains its value to the API's admissible range … the bound is
    declared once, beside the other backend-mirroring constants", i.e. the TypeScript
    ``METRIC_TIME_WINDOW_SEC_MAX`` mirrors this constant. Its twin assertion lives in
    ``src/frontend/components/governance/metric-form.schema.test.ts``; the two together
    pin the mirror, since each names the same literal independently.
"""

from src.shared.metric_conf import MAX_TIME_WINDOW_SEC, time_window_sec_error


def test_max_time_window_sec_is_the_spec_ceiling() -> None:
    """The ceiling is exactly ten years in seconds — 315_360_000.

    Spec: spec/API.md §Metric — "An integer in `[1, 315360000]` (ten years)".
    Spec: spec/feature/BACKEND.md §Metrics Service — Window bounds — "`time_window_sec`
          is an integer in `[1, 315_360_000]` — one second to ten years."
    """
    assert MAX_TIME_WINDOW_SEC == 315_360_000, (
        f"the admissible window ceiling is the spec's 315_360_000 (ten years); got "
        f"{MAX_TIME_WINDOW_SEC}. Spec: spec/API.md §Metric; spec/feature/BACKEND.md "
        "§Metrics Service — Window bounds."
    )
    assert MAX_TIME_WINDOW_SEC == 3650 * 24 * 60 * 60, (
        "…and 315_360_000 is what 'ten years' means here: 3650 days of 86400 seconds."
    )


def test_rejection_message_names_the_closed_interval_and_the_metric_type() -> None:
    """The rejection message states the closed interval and the offending type.

    Both write-boundary layers emit this one message, so the interval a client is told
    about is the interval that was enforced.

    Spec: spec/feature/BACKEND.md §Metrics Service — Window bounds — enforcement "lives at
          the write boundary only — the request schema checks create and replace bodies,
          and the service checks the *merged* `metric_conf` of a `PATCH` — each raising
          `422 INVALID_PARAMETER`".
    """
    message = time_window_sec_error("ingestion-freshness")

    assert message == (
        "metric_conf.time_window_sec must be an int in [1, 315360000] "
        "for metric_type 'ingestion-freshness'"
    ), f"unexpected rejection message: {message!r}"

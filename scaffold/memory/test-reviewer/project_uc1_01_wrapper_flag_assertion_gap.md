---
name: uc1-01-wrapper-flag-assertion-gap
description: UC1-01's mirrored-run event asserts only isinstance(wrapper, bool) while both twins' headers claim wrapper=true — API.md L364 makes the value determinate for the API-trigger path
metadata:
  type: project
---

`GET /spoke/ingestion/sources/{id}/event` rows carry a **derived** `wrapper: bool` —
`API.md` §Ingestion L364: "`true` for an event originating on a linked wrapper rather than the
source itself"; `BACKEND_SCHEMA.md` L130-132 confirms it is derived at read time, never stored.

Three artefacts disagree about the expected value:

- `tests/integration/api_wired/test_uc1_01_datahub_managed.py` module header (~L28-30) and the E2E
  twin's header (~L35-37) both claim `wrapper=true`.
- That same Python file's step-8 docstring (~L1059-1067) argues the opposite for the path it
  actually takes: `createIngestionExecutionRequest(ingestionSourceUrn=<source urn>)` books the run
  **directly on the registered source** → `wrapper=false`; only a CLI/schedule run books on a
  wrapper.
- The assertion (~L1397) splits the difference: `assert isinstance(found_event.get("wrapper"), bool)`.

So an impl that hardcodes the flag, or inverts the derivation, passes. The bool-ness assertion is
still spec-traceable (API.md says `bool`), so this is a T4 sensitivity gap, not impl-pinning.

**How to apply:** when this file is next touched, do not accept the headers and the assertion as
consistent. Either the trigger path determines the value (then assert it and fix both headers) or it
does not (then fix both headers to say so). Do not "strengthen" to `is True` from the header alone —
the docstring's mechanism argument says `false` is what the API-trigger path produces. Related:
[[uc1-event-status-anchor]], [[e2e-uc1-01-retry-doomed-step6]].

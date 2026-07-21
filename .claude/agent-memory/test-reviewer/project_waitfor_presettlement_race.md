---
name: waitfor-presettlement-race
description: react-query hooks with an initial fallback (env value, or "") make `await waitFor(...)` assertions pass pre-settlement, hiding the behavior under test
metadata:
  type: project
---

In Vitest suites over a react-query hook that returns something **before the query settles**,
`await waitFor(() => expect(result.current).toEqual({...}))` resolves on the *first* render —
before `data` is defined — whenever the expected value coincides with the pre-response value.
The assertion then proves nothing about what the response carried.

**Why:** verified by mutation on 2026-07-19 against the then-current `useDisplayLinks`, which
returned the env URL until `/spoke/common/peripheral-links` resolved. Inverting `envX || apiX`
to `apiX || envX` left 20 of 21 tests in `lib/api/peripheral-links.test.tsx` green, including the
headline "env set + API set → env wins" case. Only a test asserting an **API-sourced** field
(forcing a wait for settlement) failed.

**Current shape (issue #78, 2026-07-22):** the env plane is gone — `useDisplayLinks` resolves
`data?.<field> ?? ""` only, so the pre-response value is all-`""`. The race survives in the
mirror image: any assertion expecting `""` (unconfigured / in-flight / read-failed) is satisfied
by the first render. Those cases need a settled-state gate (`isSuccess` / `isError`) **and**
something injected whose absence is being proven — an all-`""` expectation over an
all-`""` starting state injects nothing (TESTING.md §Assertion Discipline, "Absence assertions
require injection").

**How to apply:** when reviewing a resolution / merge / fallback test over a react-query hook,
check that the assertion cannot be satisfied by the loading state, and that a negative
expectation had a competing value injected first.

Related: [[display-link-safety-spec-landed]], [[peripheral-links-db-sole-source]]

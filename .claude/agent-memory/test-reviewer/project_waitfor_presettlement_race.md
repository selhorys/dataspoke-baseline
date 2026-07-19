---
name: waitfor-presettlement-race
description: react-query hooks with an env/prop fallback make `await waitFor(...)` assertions pass pre-settlement, silently hiding precedence regressions
metadata:
  type: project
---

In Vitest suites over a react-query hook that returns a **fallback value while the query is
in flight** (e.g. `useDisplayLinks` returning the env URL until `/spoke/common/peripheral-links`
resolves), `await waitFor(() => expect(result.current).toEqual({...env values}))` resolves on
the *first* render — before `data` is defined. The assertion therefore proves nothing about how
env and API values are merged once both are present.

**Why:** verified by mutation on 2026-07-19 — inverting `envX || apiX` to `apiX || envX` in
`src/frontend/lib/api/peripheral-links.ts` left 20 of 21 tests in
`lib/api/peripheral-links.test.tsx` green, including the headline
"env set + API set → env wins" case. Only a test asserting an **API-sourced** field
(forcing a wait for settlement) failed.

**How to apply:** when reviewing a precedence / merge / override test over a react-query hook,
check that the assertion cannot be satisfied by the loading state. Ask for either a settled-state
gate (wait on a field only the API can supply, or on `isSuccess`) before the precedence assertion.
The same pattern applies to any hook with an optimistic or fallback initial return.

Related: [[display-link-safety-spec-landed]]

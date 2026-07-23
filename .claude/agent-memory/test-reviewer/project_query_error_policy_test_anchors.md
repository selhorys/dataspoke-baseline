---
name: query-error-policy-test-anchors
description: What is spec vs library behavior in the frontend Query Error Policy (issue #79) — which retry/poll assertions are load-bearing and which are dependency canaries
metadata:
  type: project
---

`spec/feature/FRONTEND_BASIC.md §Query Error Policy` (added 2026-07-23, issue #79) is the anchor
for every frontend retry/fail-fast test. Facts that decide whether an assertion there is
load-bearing:

- **"Retried up to twice" has exactly one observable form: 3 `queryFn` calls.** The unit-level
  probe of `defaultQueryRetry(n, err)` returning `true` for n=0,1 and `false` for n=2 only means
  that *because* TanStack passes a 0-based `failureCount`; the mapping is pinned solely by
  `app/providers.test.tsx`, which mounts a `useQuery` inside the real `<Providers>` and counts
  attempts. Keep that pair — the loop test alone re-encodes `failureCount < 2`.
- **Deleting `retry:` from `providers.tsx` is caught**: TanStack v5's default is `retry: 3`, so a
  fail-fast case would log 4 attempts against an expected 1.
- **`refetchInterval` keeps firing while a query is in the error state** (verified in
  `@tanstack/query-core@5.100.14` `queryObserver.#updateRefetchInterval` — it gates on `enabled`
  and a valid timeout only, never on status). So the spec's "failing fast does not stop polling /
  pages self-heal on the next poll" is library behavior; a test for it is a dependency canary
  whose real value is catching a call site that disables the query on error.
- **Three per-hook rules are spec'd exceptions, not impl detail**: `useIngestionSecrets` treats any
  `503` as final, `usePeripheralLinks` retries **once**, and failed **mutations** keep TanStack's
  no-retry default (a policy misplaced under `defaultOptions.mutations` would replay writes).
  `lib/api/*.test.ts` wrappers mostly set `retry: false`, which masks all of this — an attempt-count
  test here needs a client configured with `defaultQueryRetry` + `retryDelay: 0`.
- **`QueryErrorStateProps.error` is typed `unknown`**, so passing `error.message` instead of the
  error object typechecks and silently downgrades every peripheral error to the ordinary branch.
  Nothing at the ~18 call sites pins the onboarding branch; only
  `admin/conf/workflow-schedules-card.test.tsx` touches the ordinary branch.
- Copy that the spec actually constrains: the peripheral is *named*, admins get a link to
  `/admin/peripherals`, non-admins get **no** link, the role-specific line is withheld until the
  role resolves, and the toast is neutral rather than destructive. The sentences themselves
  ("isn't connected yet", "…then try again.") and the `datahub → "DataHub"` label map are copy.

Related: [[peripheral-links-db-sole-source]], [[waitfor-presettlement-race]]

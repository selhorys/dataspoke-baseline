---
name: frontend-role-gate-stale-me
description: useMe().isAdmin can stay true for a newly-logged-in non-admin — logout clears the zustand store but never the react-query cache, so any client-side role gate is one stale signal, not two
metadata:
  type: project
---

`src/frontend/lib/auth/use-me.ts` derives `isAdmin` as
`(store.me ?? query.data)?.role === "Admin"`, with `staleTime: 60_000` on the
`["auth","me"]` query. Logout (`components/app-shell.tsx` `handleLogout`) calls the
zustand `clear()` and `router.replace("/login")` — a **client-side** nav. Login
(`app/(public)/login/page.tsx`) is `router.replace(next)`, also client-side. The
`QueryClient` lives in `app/providers.tsx` and survives both, and nothing anywhere
calls `queryClient.clear()` / `removeQueries`.

Consequences for any surface gated on a client-side role flag:

- After logout→login as a different user in the same tab, `["auth","me"]` still holds
  the **previous** user's `Me`. Within its 60s staleTime there is no refetch at all,
  so a Reader renders with `isAdmin === true` indefinitely; outside it, the cached
  value still wins the first render(s) while the refetch is in flight, and `useMe`'s
  effect writes it into the store.
- Admin query caches survive too. A key like `["admin","api-tokens",{…}]` carries no
  caller identity, so the previous Admin's rows are served to the next user. With
  `meta: { handledInline: true }` the subsequent 403 raises no toast and react-query
  keeps the last successful `data`, so the stale admin rows stay on screen.
- `enabled: isAdmin && …` plus "the control only renders under `isAdmin`" are **not**
  two independent guards — both read the same stale signal.

**Why:** on issue #145 the frontend generator called this pair "two independent
reasons the admin query cannot fire for a non-Admin" and named it the security-
relevant assertion of the stage. The real enforcement point is the backend
(`src/api/routers/admin.py` mounts `dependencies=[Depends(require_admin)]` on the
whole router); the client gate is presentation only. Note `/admin/users` already
fires `useAdminUsers()` above its own `!isAdmin` early return, so "a non-admin never
issues an admin request" was never true app-wide.

**How to apply:** when reviewing any new role-gated fetch, reject "the flag is false
while `/auth/me` is in flight" as a sufficient argument. Ask for the caller's id in
the query key (cheap, local) and treat the missing `queryClient.clear()` on logout as
the systemic fix. Related: [[shared-response-model-unpopulated-field]].

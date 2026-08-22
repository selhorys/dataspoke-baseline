---
name: project-frontend-dep-pin-convention
description: src/frontend/package.json convention — runtime deps use ^major.minor.patch floors; flag major-only floors as supply-chain hygiene
metadata:
  type: project
---

In `src/frontend/package.json`, every runtime dependency uses a `^major.minor.patch`
floor (e.g. `zod` `^3.24.4`, `@tanstack/react-query` `^5.76.1`, `tailwind-merge` `^3.3.0`).
A bare major-only floor (e.g. `"yaml": "^2"`) is an outlier and looser than convention.

**Why:** pnpm-lock.yaml pins exact versions so current installs are deterministic, but a
major-only floor widens the auto-upgrade surface if the lockfile is ever regenerated — more
relevant for runtime deps than for `@types/*` devDependency stubs (which are already major-only
`^22` / `^19` by repo convention and are not a concern).

**How to apply:** During supply-chain review of frontend dependency diffs, flag any new
*runtime* dependency declared with a major-only floor as a low-severity hygiene finding;
suggest matching the minor.patch floor convention. Do not flag `@types/*` devDeps for this.
Confirm the lockfile (`pnpm-lock.yaml`) was updated alongside `package.json` regardless.

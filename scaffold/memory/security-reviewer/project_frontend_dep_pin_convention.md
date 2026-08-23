---
name: project-frontend-dep-pin-convention
description: src/frontend/package.json convention — runtime deps use ^major.minor.patch floors. Promoted to scaffold/roles/security-reviewer.md §5 Supply chain.
metadata:
  type: project
---

The standing rule now lives in `scaffold/roles/security-reviewer.md` §5 Supply chain: frontend
runtime dependencies use a `^major.minor.patch` floor by convention; a bare major-only floor
(e.g. `"yaml": "^2"`) is a low-severity hygiene finding. `@types/*` devDependencies are exempt.

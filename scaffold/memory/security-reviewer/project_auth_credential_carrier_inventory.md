---
name: auth-credential-carrier-inventory
description: The credential/authorisation carriers a DataSpoke auth diff must account for — the inventory now lives in spec/feature/AUTH.md; lock-order + isolation-level facts promoted there, populate_existing verification promoted to scaffold/roles/security-reviewer.md
metadata:
  type: project
---

`spec/feature/AUTH.md` §Credential reset on link and §Serialization of credential-creating writes
already document the 7-carrier invalidation table and the per-write re-validate-under-lock
pattern in more detail than this note did. The two facts this note added beyond that spec are now
promoted:

- **Lock order** (`users` → `api_tokens` → `password_reset_tokens`) and the **READ COMMITTED
  isolation dependency** → `spec/feature/AUTH.md` §Serialization of credential-creating writes
  (paragraph after the re-check table).
- **`populate_existing=True` verification** (`SessionLocal`'s `expire_on_commit=False` means a
  naive re-read returns the stale identity-mapped instance) → `scaffold/roles/security-reviewer.md`
  §2 AuthN/AuthZ.

Related: [[auth-revoke-refresh-asymmetry]], [[auth-fail-closed-spans-layers]].

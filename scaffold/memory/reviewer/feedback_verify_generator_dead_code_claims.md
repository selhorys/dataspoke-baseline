---
name: verify-generator-dead-code-claims
description: When a refactor extracts/moves code, grep that the generator's "still used elsewhere" claim is true — orphaned hooks/components are common
metadata:
  type: feedback
---

When a generator extracts or relocates UI into new components and its completion
report claims the old hooks/components are "still used elsewhere," verify it with
a repo-wide grep — do not trust the claim. Refactors that fold per-feature event
lists / panels into a unified surface routinely orphan the old data hooks and list
components, leaving them referenced only by their own definition or their own test.

**Why:** In the `/data/[urn]` unification (Stage 3), the generator asserted the
metagen `useMetagenDatasetEvents` hook, the `EventsSection` component, and
`useValidationEvents` were "still used elsewhere." A grep showed each had ZERO
production consumers post-refactor (only self-definition / self-test). The claim
was false; the code was dead. Typecheck/lint/tests all stayed green because dead
exports and their tests still compile and pass.

**How to apply:** For any extract/move refactor, run per-symbol greps:
`grep -rn '<SymbolName>' src/frontend --include='*.ts' --include='*.tsx' | cat`
(pipe to cat — see [[bash-errexit-grep-output]]). A symbol that appears only in its
own definition file (± a same-named test) is orphaned dead code — report it as a
low/medium finding even when the build is green. Per the repo's pre-release
no-compat-shim / no-historical-cruft posture, orphaned code from a rename should be
removed, not left behind.

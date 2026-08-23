---
name: sanitizer-pipeline-ordering
description: Review rule for multi-stage string sanitizers — normalize BEFORE the exact-value scrub. Promoted to scaffold/roles/security-reviewer.md §3 Secrets.
metadata:
  type: project
---

The standing rule now lives in `scaffold/roles/security-reviewer.md` §3 Secrets: normalize before
the exact-value scrub (an invisible character inside the secret defeats a scrub placed first, and
normalization then re-assembles the secret), and benchmark a sanitizer with adversarial input built
from its own character classes, not benign filler.

**Measured affix/lookbehind numbers kept for reference:** a pattern shaped
`(?<![\w-])[\w.-]{0,64}NAME[\w.-]{0,64}...` (dot in the affix but not the lookbehind) measured
6.1 us/char vs 0.13 us/char baseline — 48x; adding `.` to the lookbehind cut a 100 KB adversarial
input from 608 ms to 1.3 ms with no coverage loss.

Related: [[peripheral-health-error-redaction]], [[project-recipe-regex-trust-boundary]].

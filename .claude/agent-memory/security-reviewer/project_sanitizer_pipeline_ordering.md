---
name: sanitizer-pipeline-ordering
description: Review rule for multi-stage string sanitizers — normalize BEFORE the exact-value scrub, or an invisible char inside the secret defeats the scrub and normalization then re-assembles the secret; plus the affix/lookbehind class asymmetry that drives regex cost
metadata:
  type: project
---

Two defects recur in this repo's sanitizer pipelines. Test both by **execution**, not by reading.

**1. Stage order: normalize first, exact-scrub second.** An exact-value scrub
(`text.replace(secret, "<redacted>")`) placed *before* a control-character strip is defeated by one
invisible character inside the credential — and the strip then re-assembles the credential verbatim
in the output, so the pipeline is worse than no normalization at all. Verified vectors: `\x00`,
U+200B ZWSP, U+00AD SOFT HYPHEN (category `Cf`, so it is stripped by an `isprintable()`/`Cf` filter).
Whitespace is the exception — collapse rewrites `\n` to a space rather than removing it, so a
newline inside the secret survives *either* order with both halves in cleartext; that needs a
separately-scrubbed whitespace-collapsed rendering of the secret.

**Why:** the exact-value scrub is always documented as "the strongest layer, no naming variation
defeats it". Putting it first quietly makes that claim false.

**2. Affix class ⊋ lookbehind class ⇒ overlapping start positions.** A pattern shaped
`(?<![\w-])[\w.-]{0,64}NAME[\w.-]{0,64}...` has `.` inside the affix but not the lookbehind, so every
position after a `.` is a fresh start and the two bounded quantifiers probe 65x65 from each one.
Growth stays **linear** (the `{0,64}` bound does prevent issue #114's superlinear class), but the
constant measured 6.1 us/char vs 0.13 us/char baseline — 48x. Adding `.` to the lookbehind
(`(?<![\w.-])`) cut a 100 KB adversarial input from 608 ms to 1.3 ms with no coverage loss.

**How to apply:** benchmark a sanitizer with an input built from *its own* character classes
(dot-dense, credential-name-dense, delimiter-absent), not with benign filler — a generator's own
timing table will understate the constant by orders of magnitude. Then ask whether the call site
bounds input length: a linear cost is still an event-loop stall when the input is unbounded.
Related: [[peripheral-health-error-redaction]], [[recipe-regex-trust-boundary]].

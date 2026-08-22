---
name: resource-cap-search-worst-case-inside-the-cap
description: Reviewing a resource cap on untrusted input — sweep construct SHAPES at a fixed cap value, never re-measure the plan's example, and measure every resource the cap claims to bound
metadata:
  type: feedback
---

When a change adds a numeric cap on untrusted input (pattern size, payload
length, repeat count), do not verify it by re-running the bomb the plan quoted.
Build the most expensive input that **satisfies** every cap and measure that.
Two dimensions to sweep, not one:

1. **Size** — max out each cap and their product (patterns-per-list x
   lists-per-set x sets-per-source).
2. **Shape** — hold the cap's own metric constant and vary the *construct*. This
   is the one both the generator and the plan skipped twice on issue #114. At an
   identical `product x length` score, a literal run compiled in 0.001 s while a
   repeated empty capturing group took **464 s**, and memory per unit varied
   100x by construct. A cap whose metric does not predict cost is a heuristic
   wearing an "exact, countable limit" label.

Also measure **every** resource the cap claims to bound. Round 1 of #114 bounded
length and repeat count but not their product (memory). Round 2 bounded the
product (memory, though it certified 42 MB when 212 MB was reachable) but left
compile *time* unbounded, because the budget is only checked *between*
uninterruptible compile calls.

**How to apply:** ask "what is the cost function, and which caps appear in it?"
Check the *pre-change* implementation on the same input — if the old code was
cheap (stdlib `re`: 0.001 s / 1 MB where `regex` took 464 s / 291 MB), the
finding is a regression, not a pre-existing hole, and severity goes up. And when
the second certification of the same cap is also wrong, escalate rather than
sending a third fix pass.
Related: [[regex-bounded-matcher-facts]]

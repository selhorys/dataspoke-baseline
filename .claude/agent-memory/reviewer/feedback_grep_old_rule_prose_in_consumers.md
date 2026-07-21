---
name: grep-old-rule-prose-in-consumers
description: When a generator deletes a resolution/precedence rule, grep the OLD rule's prose across non-test consumers — generators update the implementing module and leave consumer doc comments asserting the dead rule
metadata:
  type: feedback
---

When a stage removes or flips a resolution rule (precedence order, fallback
chain, source-of-truth plane), do not stop at verifying the implementing module.
Grep the **prose of the old rule** across every non-test consumer and every shell
helper that documents the same wiring.

**Why:** generators reliably rewrite the doc comment on the module they edit and
reliably miss the copies. On the issue-#78 fix (`env-first` → API-only for the
DataHub/Langfuse display links) the hook, the runtime-config interface, the
layout, and the chart were all correct, but four consumers plus one shell helper
still carried `env-first, then GET /spoke/common/peripheral-links` and "pointing
at the in-cluster API and DataHub". That directly contradicts the spec those same
comments cite, and violates the project rule that comments describe present state.

**How to apply:** grep the distinctive phrase from the old rule
(`env-first`, `then`, `precedence`, `wins`, the removed env-var names) filtered to
non-test sources, e.g.
`grep -rn "env-first\|env plane\|precedence" --include=*.ts --include=*.tsx src/ | grep -v '\.test\.'`
plus the same phrase over `helm-charts/bin/`. A zero-hit grep on *identifiers*
(the generator's usual evidence) says nothing about prose. Related:
[[no-references-remain-brace-grep]], [[verify-generator-dead-code-claims]].

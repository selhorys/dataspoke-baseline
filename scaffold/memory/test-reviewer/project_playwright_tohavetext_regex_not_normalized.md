---
name: playwright-tohavetext-regex-not-normalized
description: Playwright toHaveText/toHaveAttribute normalize whitespace for STRING expectations but not for RegExp. Promoted to spec/TESTING.md §Selectors.
metadata:
  type: project
---

Now documented in `spec/TESTING.md` §Selectors. Confirmed safe for the shadcn `SelectTrigger`
(`{children}` first, then an icon-only `<svg>`), so `expect(trigger).toHaveText(/^Title\b.*A.*Z/)`
is sound — verify child order case-by-case, don't reject anchored regexes on principle.

Related: [[metric-view-controls-test-seams]].

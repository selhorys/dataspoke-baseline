---
name: playwright-tohavetext-regex-not-normalized
description: Playwright toHaveText/toHaveAttribute normalize whitespace for STRING expectations but not for RegExp — so ^-anchored regexes run against raw textContent
metadata:
  type: project
---

Measured in `tests/e2e/node_modules/.pnpm/playwright-core@1.60.0/.../coreBundle.js`,
`ExpectedTextMatcher.matches`:

```js
matches(text) {
  if (!this._regex) text = this.normalize(text);   // <-- normalize SKIPPED for regexes
  ...
}
```

`toHaveText` passes `normalizeWhiteSpace: true`, but that only collapses/trims the
received text when the expectation is a string or substring. A **RegExp expectation is
tested against the raw `elementText().full`**.

**Why:** it makes `/^Foo/` and `/Bar$/` on `toHaveText` sensitive to any stray leading or
trailing whitespace or newline in the element's raw text — which JSX usually strips, but
a `whitespace-pre` node, a template literal, or a text sibling before `{children}` would
reintroduce.

**How to apply:** when reviewing an `^`/`$`-anchored regex in a Playwright text
assertion, check the component's actual child order rather than assuming normalization.
Confirmed safe for the shadcn `SelectTrigger` (`{children}` first, then an icon-only
`<svg>`), so `expect(trigger).toHaveText(/^Title\b.*A.*Z/)` is sound. Not a reason to
reject anchored regexes — a reason to verify them.

Related: [[metric-view-controls-test-seams]].

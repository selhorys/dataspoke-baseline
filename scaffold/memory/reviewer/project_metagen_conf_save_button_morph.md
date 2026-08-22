---
name: metagen-conf-save-button-morph
description: "metagen conf detail now keeps Save in the HEADER slot with distinct keys (conf-save/conf-edit) — the safe variant; verify the keys survive, jsdom cannot catch a regression"
metadata:
  type: project
---

`src/frontend/app/(app)/metagen/conf/[id]/page.tsx` renders the header action slot as
`editing ? (Save[key=conf-save] + Cancel[key=conf-cancel]) : (Edit[key=conf-edit] + Run + Delete)`.
Save is `type="submit" form={CONF_FORM_ID}` in the header — **not** inside the form — and the
non-editing arm's Edit is `type="button"`. The distinct `key`s are what makes this safe: they
stop React reusing one `<button>` node as the other across the `editing` flip.

Verified 2026-08-13 (governance SQL-filter run): this arrangement is at HEAD and was untouched
by the frontend stage. An earlier review cycle had bounced a keyless version of the same
refactor; the keys are the fix that let it land.

**Why it matters:** without the keys, React morphs the reused node on Edit→Save, the browser
runs the default submit action, and clicking Edit silently PUTs. jsdom has no default-action
phase, so every Vitest spec stays green — only the real-browser Playwright repro
`tests/e2e/ground/metagen/conf-edit-no-submit.spec.ts` catches it.

**How to apply:** on any change to these header buttons (or the ontogen conf page, which uses the
same `type="submit" form=` header Save at `app/(app)/ontogen/conf/page.tsx:97`), confirm both arms
still carry distinct stable keys and that the E2E repro still covers the page. The governance
metric detail page is a different shape — its Save lives inside `MetricForm`'s own action bar and
the header arm unmounts entirely while editing, so it has no morph surface.

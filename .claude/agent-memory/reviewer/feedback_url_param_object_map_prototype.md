---
name: url-param-object-map-prototype
description: A copy/label map keyed by a URL query param must guard prototype keys — `map[code] ?? FALLBACK` renders blank for toString/constructor/__proto__; prefer a Map, since Object.hasOwn is ES2022
metadata:
  type: feedback
---

When a frontend page selects copy by indexing a plain object literal with a value
the user controls (`searchParams.get("error")`, a route param, a hash), probe the
`Object.prototype` keys before believing the fallback works: `toString`,
`constructor`, `valueOf`, `hasOwnProperty`, `__proto__`.

**Why:** `const M: Record<string, T | undefined> = {...}` inherits from
`Object.prototype`, so `M["toString"]` is a truthy *function*, not `undefined`.
`M[code] ?? FALLBACK` therefore skips the fallback and the component renders
`copy.title` / `copy.description` as `undefined` — an empty card, not the generic
wording the spec requires. TypeScript types the lookup as `T | undefined` and
never flags it; a colocated test whose "unrecognised code" case is `SOMETHING_ELSE`
passes while every prototype key is broken. Found on `/oauth-error` (#83).

**How to apply:** grep the diff for `[` indexing on a value derived from
`useSearchParams`/`useParams`. To prove it, drop a throwaway vitest file next to
the page that renders each prototype key and asserts against a sentinel so the
rendered text prints in the failure output — jsdom reproduces it exactly, since
this is plain JS semantics.

**Fix, and the second trap:** prefer `new Map<string, T>([...])` + `.get(code)`.
`Object.hasOwn` is ES2022 — `src/frontend/tsconfig.json` sets `target: ES2017`
with `lib: ["esnext"]`, so tsc accepts *any* modern built-in without downleveling
it and `pnpm typecheck` stays green while the browser floor silently rises. The
repo sets no browserslist and Next polyfills only a fixed set, so a too-new
built-in throws a TypeError and the client component renders nothing. Check
`lib`-vs-`target` skew before accepting any new built-in in a review; `new
Map([entries])` is ES2015 and already used in `lib/ontogen-graph.ts`. Related:
[[feedback_str_iterated_as_pattern_list]] — both are "the type says it is safe,
the runtime disagrees".

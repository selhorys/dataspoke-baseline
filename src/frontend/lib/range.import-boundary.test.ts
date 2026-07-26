/**
 * Static invariants over the frontend source tree for the query-path time
 * window. Three separate leaks would each reinstate the frozen upper bound (or
 * the refetch loop) with every behavioural test and `tsc` still green, so each
 * gets its own scanner here:
 *
 *   1. SYMBOL SWAP — `resolveRangeForEdit` (the EDITOR resolver, which returns a
 *      concrete closed pair) reaching a query path.
 *   2. INLINE RE-PIN — a call site mapping its end bound back onto the clock,
 *      e.g. `until: resultRange.to ?? new Date().toISOString()`.
 *   3. DROPPED MEMO / DROPPED `tz` DEP — a query-path `resolveRange` call that is
 *      no longer wrapped in `useMemo(..., [selection, tz])`.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *     "**A preset resolves to an open-ended window** — the lower bound only,
 *     with `to`/`until` omitted — so the read always reaches the present, which
 *     is what lets a 15 s-polled panel … surface records written after page
 *     load."
 *   - same section: "a preset's *lower* bound is resolved against the clock at
 *     resolution time and then held — re-derived only when the selection or the
 *     display timezone changes, or on the next visit, never per render and never
 *     per poll tick, because it participates in the query key and re-resolving
 *     it per render would mint a new key every render and spin an unbounded
 *     refetch loop."
 *   - same section: the RangePicker is "the single time-window control for every
 *     time-windowed surface (validation detail results + events, governance
 *     metric detail results + events, governance dashboard, ingestion source
 *     events, the per-dataset page's unified Events panel)".
 *
 * Why source scans rather than eight React-level component tests: that spec
 * sentence binds *every* enumerated surface, and each of the three leaks above is
 * invisible to the type-checker (`ClosedRange` is structurally assignable to
 * `RangeValue`; an inline `?? new Date()` type-checks; a missing dep is not a
 * type error). O(1) invariants cover all current and future call sites;
 * replicating a React-level params assertion per page would not, and a
 * behavioural test cannot observe a dropped `tz` dep at all where the resolved
 * bounds happen to be tz-independent (datetime granularity).
 *
 * Scanner discipline: every scanner below is exercised on injected positive AND
 * negative source snippets inside the same test that asserts over the real tree,
 * so an assertion can never pass because the regex matched nothing.
 *
 * The import scan reads import/export statements only (never bare identifier
 * occurrences), so a doc comment naming the symbol — e.g. "do NOT use
 * resolveRangeForEdit here" — cannot trip it.
 */
import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/** `src/frontend/` — this file lives in `src/frontend/lib/`. */
const FRONTEND_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

const SKIP_DIRS = new Set([
  "node_modules",
  ".next",
  ".git",
  ".claude",
  "public",
  "coverage",
]);

/** Every non-test `.ts`/`.tsx` file under `src/frontend/`, repo-relative. */
function walkSources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      walkSources(full, out);
      continue;
    }
    if (!/\.tsx?$/.test(entry.name)) continue;
    // Test files legitimately import both resolvers — they exist to exercise
    // them. Only production modules are subject to the boundary.
    if (/\.test\.tsx?$/.test(entry.name)) continue;
    out.push(full);
  }
  return out;
}

const SOURCE_FILES = walkSources(FRONTEND_ROOT);

/** POSIX-style path relative to `src/frontend/`, for stable assertions. */
function rel(file: string): string {
  return path.relative(FRONTEND_ROOT, file).split(path.sep).join("/");
}

/** Read a repo-relative source path back off disk. */
function readSource(relPath: string): string {
  return readFileSync(path.join(FRONTEND_ROOT, relPath), "utf8");
}

// ── Scanner 1: named bindings of lib/range ────────────────────────────────────
//
// Covers BOTH channels that create a named binding from another module:
//   import { resolveRangeForEdit } from "@/lib/range";
//   export { resolveRangeForEdit } from "@/lib/range";   // barrel re-export
// The re-export form is included deliberately: a barrel that republishes the
// editor resolver is a laundering channel for the symbol-swap leak, so it counts
// as a boundary violation at the barrel itself.

const NAMED_BINDING_RE =
  /(?:import|export)\s+(?:type\s+)?\{([^}]*)\}\s*from\s*["']([^"']+)["']/g;

/** True when a module specifier resolves to `lib/range` (alias or relative). */
function isRangeModule(spec: string): boolean {
  return spec === "@/lib/range" || /(^|\/)range$/.test(spec);
}

/** The names a source text binds from `lib/range` (imported or re-exported). */
function rangeBindingsIn(source: string): Set<string> {
  const names = new Set<string>();
  for (const match of source.matchAll(NAMED_BINDING_RE)) {
    if (!isRangeModule(match[2])) continue;
    for (const raw of match[1].split(",")) {
      const name = raw
        .trim()
        .replace(/^type\s+/, "")
        .split(/\s+as\s+/)[0]
        .trim();
      if (name) names.add(name);
    }
  }
  return names;
}

/** Files that bind `symbol` from `lib/range`, by import or by re-export. */
function importersOf(symbol: string): string[] {
  return SOURCE_FILES.filter((f) =>
    rangeBindingsIn(readFileSync(f, "utf8")).has(symbol),
  )
    .map(rel)
    .sort();
}

// ── Scanner 2: wholesale (unnamed) access to lib/range ────────────────────────
//
// Three forms pull the module in without naming a binding, so scanner 1 cannot
// see what is used:
//   import * as range from "@/lib/range";        → range.resolveRangeForEdit(…)
//   export * from "@/lib/range";                 → barrel republishes everything
//   const range = await import("@/lib/range");   → dynamic
//
// ACCEPTED RESIDUALS (not scanned, recorded so the totality claim stays
// accurate): a CommonJS `require("@/lib/range")` and a dynamic import with a
// computed specifier (`import(someVar)`). Neither appears in this tree, neither
// is expressible in the app's ESM + path-alias setup without extra machinery,
// and a source scan cannot resolve a computed specifier anyway.

const NAMESPACE_IMPORT_RE = /import\s+\*\s+as\s+\w+\s+from\s*["']([^"']+)["']/g;
const STAR_REEXPORT_RE = /export\s+\*\s*(?:as\s+\w+\s*)?from\s*["']([^"']+)["']/g;
const DYNAMIC_IMPORT_RE = /\bimport\s*\(\s*["']([^"']+)["']\s*\)/g;

/** Does this source text pull `lib/range` in without naming its bindings? */
function hasWholesaleRangeAccess(source: string): boolean {
  return [NAMESPACE_IMPORT_RE, STAR_REEXPORT_RE, DYNAMIC_IMPORT_RE].some((re) =>
    [...source.matchAll(re)].some((m) => isRangeModule(m[1])),
  );
}

/** Files that dodge the named-binding scan by pulling the module in wholesale. */
function wholesaleAccessors(): string[] {
  return SOURCE_FILES.filter((f) =>
    hasWholesaleRangeAccess(readFileSync(f, "utf8")),
  )
    .map(rel)
    .sort();
}

// ── Scanner 3: memoized query-path resolution ─────────────────────────────────
//
// Every query-path call must read
// `useMemo(() => resolveRange(sel, granularity, tz), [sel, tz])`. Two distinct
// regressions are detectable from the source shape alone: the wrapper being
// dropped (call count > memo count) and a dep falling out of the array.

interface MemoizedResolve {
  /** Positional arguments to `resolveRange`, as written. */
  args: string[];
  /** Identifiers listed in the `useMemo` dependency array. */
  deps: string[];
}

interface ResolveRangeUsage {
  /** Every `resolveRange(` call in the text (an import binding has no paren). */
  callCount: number;
  memoized: MemoizedResolve[];
}

const RESOLVE_CALL_RE = /\bresolveRange\(/g;
const MEMOIZED_RESOLVE_RE =
  /useMemo\(\s*\(\)\s*=>\s*resolveRange\(([^)]*)\)\s*,\s*\[([^\]]*)\]\s*,?\s*\)/g;

function splitList(raw: string): string[] {
  return raw
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

function resolveRangeUsage(source: string): ResolveRangeUsage {
  return {
    callCount: [...source.matchAll(RESOLVE_CALL_RE)].length,
    memoized: [...source.matchAll(MEMOIZED_RESOLVE_RE)].map((m) => ({
      args: splitList(m[1]),
      deps: splitList(m[2]),
    })),
  };
}

// ── Scanner 4: clock-pinned end bound ─────────────────────────────────────────
//
// The inline re-pin: an end-bound property (`to:` for the convention endpoints,
// `until:` for validation `attr/validation/result`) whose value reads the clock.
// `until: resultRange.to ?? new Date().toISOString()` restores exactly the
// frozen upper bound the open window exists to remove, and type-checks.

const CLOCK_END_BOUND_RE =
  /\b(?:to|until)\s*:[^,;{}]*(?:new\s+Date\s*\(\s*\)|Date\s*\.\s*now\s*\(\s*\))/g;

/** End-bound mappings whose value is derived from the clock, normalized. */
function clockPinnedEndBounds(source: string): string[] {
  return [...source.matchAll(CLOCK_END_BOUND_RE)].map((m) =>
    m[0].replace(/\s+/g, " ").trim(),
  );
}

/**
 * The healthy shape of the same mapping — an end-bound param fed straight from
 * a resolved range's upper bound (`to: range.to`, `until: resultRange.to`).
 * Used as the positive leg: it locates the places where a clock re-pin COULD be
 * written, so "no clock-pinned end bound" is an assertion about the value rather
 * than about the property having disappeared.
 */
const END_BOUND_MAPPING_RE = /\b(?:to|until)\s*:\s*[A-Za-z_$][\w$]*\.to\b/;

/** Files that map some object's `.to` onto a `to:`/`until:` property. */
function endBoundMappingSites(): string[] {
  return SOURCE_FILES.filter((f) =>
    END_BOUND_MAPPING_RE.test(readFileSync(f, "utf8")),
  )
    .map(rel)
    .sort();
}

// The spec sentence quoted in the header enumerates the time-windowed surfaces
// the RangePicker drives; these are their query call sites. They are the
// backstop for the absence assertions below: the same scanners must find
// `resolveRange` (memoized, tz-dependent) at each of them, otherwise an
// empty/broken scanner would make "nobody imports resolveRangeForEdit" and
// "nobody re-pins the end bound" trivially true.
const SPEC_ENUMERATED_QUERY_SITES = [
  // "validation detail results + events"
  "components/validation/validation-data-panel.tsx",
  // "governance metric detail results + events"
  "app/(app)/governance/metrics/[id]/page.tsx",
  // "governance dashboard"
  "app/(app)/governance/dashboard/page.tsx",
  // "ingestion source events"
  "app/(app)/ingestion/sources/[id]/page.tsx",
  // "the per-dataset page's unified Events panel"
  "components/events-panel.tsx",
  // spec/feature/FRONTEND_METAGEN.md §Conf create / detail: the per-conf event
  // table has "a `datetime` [RangePicker](FRONTEND_BASIC.md#shared-component-notes)
  // driving `from`/`to`"; §Result rollup gives the cross-conf event feed the
  // same `datetime` RangePicker.
  "app/(app)/metagen/conf/[id]/page.tsx",
  "app/(app)/metagen/result/page.tsx",
];

// Where the resolved upper bound is actually mapped onto a query param. These
// are the sites an inline re-pin would be written at — the "8 call sites" the
// open-window change touches. It is NOT the same list as the surfaces above:
// the governance dashboard resolves the range once and hands it to each
// <MetricCard>, which is where that surface's `to:` mapping lives.
const END_BOUND_MAPPING_SITES = [
  "components/validation/validation-data-panel.tsx", // until: resultRange.to
  "app/(app)/governance/metrics/[id]/page.tsx", // to: resultRange.to + to: eventRange.to
  "components/governance/metric-card.tsx", // governance dashboard, per card
  "app/(app)/ingestion/sources/[id]/page.tsx", // to: range.to
  "components/events-panel.tsx", // to: range.to
  "app/(app)/metagen/conf/[id]/page.tsx", // to: range.to
  "app/(app)/metagen/result/page.tsx", // to: range.to
];

/**
 * `lib/range.ts` also matches END_BOUND_MAPPING_RE (`return { from: sel.from,
 * to: sel.to }`), but that is the resolver constructing its own return value,
 * not a call site mapping a window onto query params. Excluded by path so the
 * totality assertion below stays exact.
 */
const NON_QUERY_END_BOUND_FILES = ["lib/range.ts"];

describe("lib/range resolver import boundary", () => {
  it("scans a real, non-empty source tree (harness backstop)", () => {
    // If the walker silently returned nothing, every absence assertion below
    // would pass for the wrong reason.
    expect(SOURCE_FILES.map(rel)).toContain("lib/range.ts");
    expect(SOURCE_FILES.map(rel)).toContain("components/range-picker.tsx");
  });

  it("finds resolveRange at every spec-enumerated query surface (positive leg)", () => {
    // Proves the import parser genuinely detects this symbol family, which is
    // what makes the resolveRangeForEdit assertion below discriminating.
    expect(importersOf("resolveRange")).toEqual(
      expect.arrayContaining([...SPEC_ENUMERATED_QUERY_SITES].sort()),
    );
  });

  it("keeps the enumerated query-site list total (no unregistered call site)", () => {
    // Exact equality, not containment: a new time-windowed surface must be added
    // to SPEC_ENUMERATED_QUERY_SITES, which is what subjects it to the
    // memoization and clock-pinning scans below. Without this, a new page could
    // ship an unmemoized or re-pinned window and every scan would still be green.
    expect(importersOf("resolveRange")).toEqual(
      [...SPEC_ENUMERATED_QUERY_SITES].sort(),
    );
  });

  it("imports resolveRangeForEdit into the RangePicker editor and nowhere else", () => {
    // spec/feature/FRONTEND_BASIC.md §shared-component-notes: a preset resolves
    // open above on every time-windowed surface. `resolveRangeForEdit` returns
    // the closed pair the picker's calendars need; letting it onto a query path
    // restores exactly the frozen upper bound the open window exists to remove,
    // and the type-checker cannot see it (ClosedRange is assignable to
    // RangeValue).
    //
    // Injected legs first: the scanner must see BOTH channels it now covers, so
    // the single-entry result below cannot pass on a regex that missed one.
    expect(
      rangeBindingsIn('import { resolveRangeForEdit } from "@/lib/range";'),
    ).toContain("resolveRangeForEdit");
    expect(
      rangeBindingsIn('export { resolveRangeForEdit } from "@/lib/range";'),
    ).toContain("resolveRangeForEdit");
    expect(
      rangeBindingsIn('import { resolveRangeForEdit } from "@/lib/other";'),
    ).not.toContain("resolveRangeForEdit");

    expect(importersOf("resolveRangeForEdit")).toEqual([
      "components/range-picker.tsx",
    ]);
  });

  it("has no wholesale import/re-export of lib/range that could dodge the scan", () => {
    // `import * as range from "@/lib/range"` followed by
    // `range.resolveRangeForEdit(...)`, `export * from "@/lib/range"` in a
    // barrel, and `await import("@/lib/range")` would each be invisible to the
    // named-binding parser above. Nothing in the tree does this today; keep it
    // that way so the boundary assertion stays total (modulo the accepted
    // residuals recorded at the scanner).
    //
    // Injected positive legs first, so the empty result below cannot pass on a
    // regex that matches nothing.
    expect(
      hasWholesaleRangeAccess('import * as range from "@/lib/range";'),
    ).toBe(true);
    expect(hasWholesaleRangeAccess('export * from "@/lib/range";')).toBe(true);
    expect(hasWholesaleRangeAccess('export * as range from "./range";')).toBe(
      true,
    );
    expect(
      hasWholesaleRangeAccess('const r = await import("@/lib/range");'),
    ).toBe(true);

    // Negative legs: other modules, and the named form scanner 1 already covers.
    expect(hasWholesaleRangeAccess('import * as z from "zod";')).toBe(false);
    expect(hasWholesaleRangeAccess('export * from "@/lib/other";')).toBe(false);
    expect(hasWholesaleRangeAccess('await import("@/lib/other");')).toBe(false);
    expect(
      hasWholesaleRangeAccess('import { resolveRange } from "@/lib/range";'),
    ).toBe(false);

    expect(wholesaleAccessors()).toEqual([]);
  });

  it("keeps the RangePicker off the query resolver", () => {
    // The converse leak: seeding the editor from the open resolver would leave
    // the end-day calendar and end-time field with no bound to render.
    expect(importersOf("resolveRange")).not.toContain(
      "components/range-picker.tsx",
    );
  });
});

// ---------------------------------------------------------------------------
// Call-site shape invariants.
//
// The import boundary above only proves the RIGHT resolver is in scope. These
// two scans prove the call site USES it the way the spec requires — open above,
// and stable across renders — at every enumerated surface. They are what closes
// the "the bug survives at 7 of 8 call sites" gap: `components/events-panel.tsx`
// carries a behavioural params test, the other seven are covered here.
// ---------------------------------------------------------------------------
describe("lib/range query-path call-site shape", () => {
  it("memoizes every query-path resolveRange call on [selection, tz]", () => {
    // spec/feature/FRONTEND_BASIC.md §shared-component-notes: "a preset's
    // *lower* bound is resolved against the clock at resolution time and then
    // held — re-derived only when the selection or the display timezone changes,
    // or on the next visit, never per render and never per poll tick, because it
    // participates in the query key and re-resolving it per render would mint a
    // new key every render and spin an unbounded refetch loop."
    //
    // `[selection, tz]` is exactly that sentence's re-derivation trigger set.
    //
    // Injected legs — the scanner must accept the good shape and reject each
    // regression, otherwise the tree assertions below prove nothing.
    const good = resolveRangeUsage(
      'const range = useMemo(() => resolveRange(sel, "date", tz), [sel, tz]);',
    );
    expect(good.callCount).toBe(1);
    expect(good.memoized).toHaveLength(1);
    expect(good.memoized[0].args[0]).toBe("sel");
    expect(good.memoized[0].deps).toEqual(["sel", "tz"]);

    // Regression A — the useMemo wrapper deleted (a call the scan sees but
    // cannot pair with a memo).
    const droppedMemo = resolveRangeUsage(
      'const range = resolveRange(sel, "date", tz);',
    );
    expect(droppedMemo.callCount).toBe(1);
    expect(droppedMemo.memoized).toHaveLength(0);

    // Regression B — `tz` dropped from the dep array (a stale window after the
    // user flips the global timezone preference).
    const droppedTz = resolveRangeUsage(
      'const range = useMemo(() => resolveRange(sel, "date", tz), [sel]);',
    );
    expect(droppedTz.memoized).toHaveLength(1);
    expect(droppedTz.memoized[0].deps).not.toContain("tz");

    // The real tree.
    for (const site of SPEC_ENUMERATED_QUERY_SITES) {
      const usage = resolveRangeUsage(readSource(site));
      // Positive leg per file: the site really does call the query resolver.
      expect(usage.callCount, `${site}: no resolveRange call found`).toBeGreaterThan(0);
      // Every call is memoized — no unwrapped call slipped in alongside.
      expect(usage.memoized.length, `${site}: unmemoized resolveRange call`).toBe(
        usage.callCount,
      );
      for (const memo of usage.memoized) {
        expect(memo.deps, `${site}: tz missing from deps`).toContain("tz");
        // The selection identifier (first positional arg) must be a dep too.
        expect(memo.deps, `${site}: selection missing from deps`).toContain(
          memo.args[0],
        );
      }
    }
  });

  it("never re-pins a query end bound to the clock", () => {
    // spec/feature/FRONTEND_BASIC.md §shared-component-notes: "**A preset
    // resolves to an open-ended window** — the lower bound only, with `to`/
    // `until` omitted — so the read always reaches the present". Defaulting the
    // absent bound back onto the clock at the call site
    // (`until: range.to ?? new Date().toISOString()`) re-freezes it at mount and
    // reinstates the bug, while type-checking cleanly.
    //
    // Injected positive legs — a scanner that matched nothing would make the
    // empty result over the tree meaningless.
    expect(
      clockPinnedEndBounds(
        "const p = { until: resultRange.to ?? new Date().toISOString() };",
      ),
    ).toEqual(["until: resultRange.to ?? new Date()"]);
    expect(
      clockPinnedEndBounds("const p = { to: range.to ?? Date.now() };"),
    ).toEqual(["to: range.to ?? Date.now()"]);
    // Multi-line form of the same re-pin.
    expect(
      clockPinnedEndBounds(
        "const p = {\n  until:\n    resultRange.to ?? new Date().toISOString(),\n};",
      ),
    ).toHaveLength(1);

    // Negative legs — the shapes the tree actually uses must NOT trip it.
    expect(
      clockPinnedEndBounds("const p = { until: resultRange.to, limit: 1000 };"),
    ).toEqual([]);
    expect(clockPinnedEndBounds("const p = { to: range.to };")).toEqual([]);
    // A clock read that is not bound to an end-bound property is none of this
    // scanner's business (lib/range itself is built on `new Date()`).
    expect(clockPinnedEndBounds("const now = new Date();")).toEqual([]);
    expect(clockPinnedEndBounds("const from = Date.now() - 86400000;")).toEqual(
      [],
    );

    // Totality: the enumerated mapping sites are exactly the places in the tree
    // that feed a `to:`/`until:` param from a resolved window. A new one must be
    // registered here — and is caught by the tree-wide sweep below regardless.
    expect(
      endBoundMappingSites().filter(
        (f) => !NON_QUERY_END_BOUND_FILES.includes(f),
      ),
    ).toEqual([...END_BOUND_MAPPING_SITES].sort());

    // The real tree, per mapping site.
    for (const site of END_BOUND_MAPPING_SITES) {
      const source = readSource(site);
      // Positive leg per file: the site really does map an end bound, so the
      // absence assertion is about the VALUE, not about the property being gone.
      expect(source, `${site}: no to:/until: bound mapping found`).toMatch(
        END_BOUND_MAPPING_RE,
      );
      expect(
        clockPinnedEndBounds(source),
        `${site}: end bound re-pinned to the clock`,
      ).toEqual([]);
    }

    // …and tree-wide, so a re-pin written at a brand-new surface is caught even
    // before that surface is registered above.
    const treeWide = SOURCE_FILES.flatMap((f) =>
      clockPinnedEndBounds(readFileSync(f, "utf8")).map(
        (hit) => `${rel(f)}: ${hit}`,
      ),
    );
    expect(treeWide).toEqual([]);
  });
});

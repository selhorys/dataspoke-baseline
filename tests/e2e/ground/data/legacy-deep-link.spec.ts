/**
 * Ground spec: the retired per-feature dataset routes redirect to `/data/[urn]`
 * with the URN intact.
 *
 * `/ingestion/data/[urn]`, `/validation/data/[urn]` and `/metagen/data/[urn]` are
 * redirect shells preserving old deep links. Each one URL-decodes the `[urn]`
 * segment Next.js hands it and re-encodes it exactly once onto the target, so a
 * single-encoded legacy link must come to rest on the single-encoded unified
 * route — not on a double-encoded (or under-encoded) one. Nothing else in
 * tests/e2e/ navigates to these routes, so an encoding regression on a deep link
 * would otherwise be caught by no test.
 *
 * The redirect is asserted mechanism-agnostically (server 302 or client push):
 * the resting URL, the round-trip of the URN through it, and the hub header the
 * URN drives are the contract.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (`/data/[urn]`) — "It
 *   supersedes the former per-feature detail routes — `/ingestion/data/[urn]`,
 *   `/validation/data/[urn]`, and `/metagen/data/[urn]` now **redirect** here
 *   (preserving deep links)."
 * spec: spec/feature/FRONTEND_VALIDATION.md §Routes — `| /validation/data/[urn] |
 *   Redirect to the unified /data/[urn] page (deep-link preserved) | — |`
 * spec: spec/feature/FRONTEND_INGESTION.md §Routes — `| /ingestion/data/[urn] |
 *   Redirect to the unified per-dataset page /data/[urn] (deep-link preserved) | — |`
 * spec: spec/feature/FRONTEND_METAGEN.md §Routes — `| /metagen/data/[urn] |
 *   Redirect to the unified /data/[urn] page (deep-link preserved) | — |`
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test.
 */

import { test, expect, IMAZON_URNS } from "../../fixtures/index";

// A seeded Imazon dataset that exists in DataHub, so the hub it lands on renders
// real content rather than an error state.
const DATASET_URN = IMAZON_URNS.titleMaster;

// The legacy link shape: the URN encoded exactly once into one path segment.
const SINGLE_ENCODED = encodeURIComponent(DATASET_URN);

// Each retired per-feature dataset route. Listed explicitly (not derived) so the
// set of routes under test is readable at a glance.
const LEGACY_ROUTES = [
  { feature: "validation", path: `/validation/data/${SINGLE_ENCODED}` },
  { feature: "ingestion", path: `/ingestion/data/${SINGLE_ENCODED}` },
  { feature: "metagen", path: `/metagen/data/${SINGLE_ENCODED}` },
] as const;

for (const { feature, path } of LEGACY_ROUTES) {
  test(`/${feature}/data/[urn] — a single-encoded deep link rests on /data/[urn] with the URN intact`, async ({
    page,
    adminApi,
  }) => {
    await page.goto(path);

    // -- UI assertion: the resting URL is the unified hub route --
    // spec: FRONTEND_BASIC.md §Per-dataset page — the per-feature routes redirect here.
    // Predicate (not a regex) because the legacy path also contains "/data/": only a
    // pathname that STARTS with /data/ means the redirect completed. Deliberately
    // agnostic about the segment's encoding so an encoding regression fails on the
    // explicit round-trip assertion below rather than on this wait's timeout.
    await page.waitForURL((url) => url.pathname.startsWith("/data/"), { timeout: 15_000 });
    await expect(page).not.toHaveURL(/\/login/);

    // -- Core assertion: the URN survived the redirect encoded exactly once --
    // `pathname` preserves percent-encoding, so decoding the single segment once
    // must reproduce the URN verbatim. A double-encode leaves `urn%3Ali…` after one
    // decode; an over-eager decode-then-encode mangles the segment the same way.
    const restingPath = new URL(page.url()).pathname;
    expect(restingPath.startsWith("/data/")).toBe(true);
    const restingSegment = restingPath.slice("/data/".length);
    expect(restingSegment).not.toContain("/"); // one segment, not a nested path
    expect(decodeURIComponent(restingSegment)).toBe(DATASET_URN);

    // -- UI assertion: the hub header renders that same URN --
    // The header is driven by the `[urn]` route param, so an encoding regression
    // that survives the URL check would still surface as a mangled heading.
    // src/frontend/app/(app)/data/[urn]/page.tsx — <h1>{datasetUrn}</h1>
    await expect(
      page.getByRole("heading", { name: DATASET_URN, exact: true }),
    ).toBeVisible({ timeout: 15_000 });

    // -- Backend sanity probe (NOT the dual-confirmation leg) --
    // GET on a URN echoes that URN back, so this cannot corroborate the redirect; it only
    // confirms the dataset the deep link points at resolves at all (200, not 404). The
    // load-bearing checks are the resting-path round-trip and the hub header above.
    // spec: API.md §Data Resource — GET /spoke/common/data/{dataset_urn}
    const probe = await adminApi.get(
      `/api/v1/spoke/common/data/${encodeURIComponent(DATASET_URN)}`,
    );
    expect(probe.ok(), `dataset probe failed: ${await probe.text()}`).toBeTruthy();
    const dataset = (await probe.json()) as { urn: string };
    expect(dataset.urn).toBe(DATASET_URN);
  });
}

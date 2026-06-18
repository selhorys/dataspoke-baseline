/**
 * Ground spec: /metagen/uncovered — undocumented-datasets view + include_disallowed toggle.
 *
 * Narrow per-page flow: the page renders the uncovered table (read-only) from
 * GET /spoke/metagen/uncovered; toggling the include_disallowed checkbox flips the
 * query param off→on, widening the result set from no_conf_match to also include
 * boundary_blocked rows. Proven by capturing the actual GET requests the page fires.
 *
 * Independent: no seeding required — exercises the toggle's request wiring and the
 * reason-classification invariant, both of which hold against the seeded baseline.
 *
 * spec: spec/feature/FRONTEND_METAGEN.md §Uncovered — GET /spoke/metagen/uncovered
 *   with include_disallowed toggle: off shows no_conf_match only, on additionally
 *   shows boundary_blocked rows; read-only; each row links to its dataset page
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role
 */

import { test, expect } from "../../fixtures/index";

test("/metagen/uncovered — include_disallowed toggle flips the query param off→on", async ({
  page,
  adminApi,
}) => {
  // Capture GET /spoke/metagen/uncovered requests to prove the toggle is wired.
  const uncoveredRequests: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (req.method() === "GET" && url.includes("/spoke/metagen/uncovered")) {
      uncoveredRequests.push(url);
    }
  });

  await page.goto("/metagen/uncovered");
  await expect(page).not.toHaveURL(/\/login/);

  // -- Heading + toggle present --
  // uncovered/page.tsx: <h1>Uncovered datasets</h1>; Checkbox id="uncovered-include-disallowed"
  await expect(
    page.getByRole("heading", { name: "Uncovered datasets", exact: true }),
  ).toBeVisible({ timeout: 15_000 });
  const toggle = page.locator("#uncovered-include-disallowed");
  await expect(toggle).toBeVisible({ timeout: 10_000 });
  await expect(toggle).not.toBeChecked();

  // The initial (off) request must NOT carry include_disallowed=true.
  await expect.poll(() => uncoveredRequests.length, { timeout: 15_000 }).toBeGreaterThan(0);
  const offRequests = [...uncoveredRequests];
  expect(offRequests.every((u) => !u.includes("include_disallowed=true"))).toBe(true);

  // -- Gesture: enable include_disallowed --
  await toggle.click();
  await expect(toggle).toBeChecked();

  // -- The toggle fires a new request carrying include_disallowed=true --
  // uncovered/page.tsx: useMetagenUncovered(includeDisallowed) → ?include_disallowed=true
  await expect
    .poll(() => uncoveredRequests.some((u) => u.includes("include_disallowed=true")), {
      timeout: 15_000,
    })
    .toBe(true);

  // -- Backend invariant (dual confirmation): off ⊆ on, and every reason is in the
  //    documented classification set; off-set carries only no_conf_match. --
  // spec: FRONTEND_METAGEN.md §Uncovered
  const offResp = await adminApi.get("/api/v1/spoke/metagen/uncovered");
  expect(offResp.status()).toBe(200);
  const offBody = (await offResp.json()) as {
    datasets: Array<{ dataset_urn: string; reason: string }>;
    total_count: number;
  };
  for (const row of offBody.datasets) {
    expect(row.reason).toBe("no_conf_match");
  }

  const onResp = await adminApi.get("/api/v1/spoke/metagen/uncovered?include_disallowed=true");
  expect(onResp.status()).toBe(200);
  const onBody = (await onResp.json()) as {
    datasets: Array<{ dataset_urn: string; reason: string }>;
    total_count: number;
  };
  for (const row of onBody.datasets) {
    expect(["no_conf_match", "boundary_blocked"]).toContain(row.reason);
  }
  expect(onBody.total_count).toBeGreaterThanOrEqual(offBody.total_count);
});

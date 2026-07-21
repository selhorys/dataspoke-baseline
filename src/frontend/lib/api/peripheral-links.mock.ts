/**
 * Test-only module replacement for `./peripheral-links`.
 *
 * `DatahubDatasetLink` and `EvidenceLink` are shared leaf components, so any
 * test rendering a table containing them transitively depends on the
 * peripheral-links query. Suites that do not mount a QueryClientProvider — and
 * that assert nothing about link resolution — use this replacement.
 *
 * It reports every link as unconfigured, which is what the real hook resolves
 * whenever the peripheral-links read has not (yet) produced a URL. Those suites
 * therefore see the "render no link" branch and can assert on the rest of the
 * row. Resolution from the API response, the safe-URL degradation, and the
 * cross-row request de-duplication are covered against the real hook in
 * `peripheral-links.test.tsx`.
 *
 * The return type is pinned to the real module's, so a signature change to
 * `useDisplayLinks` fails typecheck here rather than silently passing in every
 * suite that mocks it.
 *
 * Usage (the factory must stay async so the import is not hoisted):
 *
 *   vi.mock("@/lib/api/peripheral-links", async () =>
 *     (await import("@/lib/api/peripheral-links.mock")).unconfiguredPeripheralLinksModule(),
 *   );
 */

import type { DisplayLinks } from "@/lib/api/peripheral-links";

type PeripheralLinksModule = typeof import("@/lib/api/peripheral-links");

/**
 * Deliberately omits `usePeripheralLinks`: nothing consumes the raw query
 * directly today, and a stub would have to be cast past `UseQueryResult`,
 * defeating the drift guard. A future direct consumer fails loudly here.
 */
export function unconfiguredPeripheralLinksModule(): Pick<
  PeripheralLinksModule,
  "PERIPHERAL_LINKS_QUERY_KEY" | "useDisplayLinks"
> {
  return {
    PERIPHERAL_LINKS_QUERY_KEY: ["peripheral-links"],
    useDisplayLinks: (): DisplayLinks => ({
      datahubUrl: "",
      langfuseUrl: "",
      langfuseProjectId: "",
    }),
  };
}

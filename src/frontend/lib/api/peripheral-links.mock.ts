/**
 * Test-only module replacement for `./peripheral-links`.
 *
 * `DatahubDatasetLink` and `EvidenceLink` are shared leaf components, so any
 * test rendering a table containing them transitively depends on the
 * peripheral-links query. Suites that do not mount a QueryClientProvider — and
 * that assert nothing about link resolution — use this replacement.
 *
 * It resolves the **env plane only** (the real hook's env-first branch), so
 * those suites keep behaving exactly as they did before the hook existed. The
 * API plane, the env/API merge precedence, and the cross-row request
 * de-duplication are covered against the real hook in `peripheral-links.test.tsx`.
 *
 * The return type is pinned to the real module's, so a signature change to
 * `useDisplayLinks` fails typecheck here rather than silently passing in every
 * suite that mocks it.
 *
 * Usage (the factory must stay async so the import is not hoisted):
 *
 *   vi.mock("@/lib/api/peripheral-links", async () =>
 *     (await import("@/lib/api/peripheral-links.mock")).envOnlyPeripheralLinksModule(),
 *   );
 */

import { getRuntimeConfig } from "@/lib/runtime-config";
import { sanitizeDisplayUrl, sanitizeProjectId } from "@/lib/safe-url";
import type { DisplayLinks } from "@/lib/api/peripheral-links";

type PeripheralLinksModule = typeof import("@/lib/api/peripheral-links");

/**
 * Deliberately omits `usePeripheralLinks`: nothing consumes the raw query
 * directly today, and a stub would have to be cast past `UseQueryResult`,
 * defeating the drift guard. A future direct consumer fails loudly here.
 */
export function envOnlyPeripheralLinksModule(): Pick<
  PeripheralLinksModule,
  "PERIPHERAL_LINKS_QUERY_KEY" | "useDisplayLinks"
> {
  return {
    PERIPHERAL_LINKS_QUERY_KEY: ["peripheral-links"],
    useDisplayLinks: (): DisplayLinks => {
      const config = getRuntimeConfig();
      return {
        datahubUrl: sanitizeDisplayUrl(config.datahubUrl),
        langfuseUrl: sanitizeDisplayUrl(config.langfuseUrl),
        langfuseProjectId: sanitizeProjectId(config.langfuseProjectId),
      };
    },
  };
}

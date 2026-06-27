"use client";

import { useEffect, useMemo, useState } from "react";
import { MetagenDatasetTable } from "@/components/metagen/dataset-table";
import { MetagenEventTable } from "@/components/metagen/metagen-event-table";
import {
  useMetagenConfList,
  useMetagenEvents,
} from "@/lib/api/metagen";
import { resolveRange } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { PageHeader } from "@/components/page-header";

export default function MetagenResultPage() {
  const [eventOffset, setEventOffset] = useState(0);
  const [eventLimit, setEventLimit] = useState(DEFAULT_PAGE_SIZE);

  const tz = useDisplayTz();
  const { selection: sel, setSelection: setSel } = usePersistedRangeState(
    RANGE_KEYS.metagenResultEvents,
  );
  const range = useMemo(() => resolveRange(sel, "datetime", tz), [sel, tz]);

  // Confs drive the rollup's conf_id filter select. Read a generous first page.
  const { data: confsData } = useMetagenConfList({ offset: 0, limit: 100 });

  const { data: events } = useMetagenEvents({
    from: range.from,
    to: range.to,
    offset: eventOffset,
    limit: eventLimit,
  });

  useEffect(() => {
    setEventOffset(0);
  }, [sel]);

  return (
    <div className="space-y-6">
      <PageHeader title="Result rollup" />

      <section className="rounded-lg border p-5">
        <h2 className="mb-4 text-sm font-medium">datasets (per-dataset rollup)</h2>
        <MetagenDatasetTable confs={confsData?.confs ?? []} />
      </section>

      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">Run events (cross-conf)</h2>
        <MetagenEventTable
          events={events?.events ?? []}
          range={sel}
          onRangeChange={setSel}
          tz={tz}
          page={{
            offset: eventOffset,
            limit: eventLimit,
            totalCount: events?.total_count ?? 0,
          }}
          onOffset={setEventOffset}
          onLimit={setEventLimit}
        />
      </section>
    </div>
  );
}

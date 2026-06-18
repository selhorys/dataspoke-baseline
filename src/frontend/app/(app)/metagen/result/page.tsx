"use client";

import { useEffect, useMemo, useState } from "react";
import { QueueTable } from "@/components/metagen/queue-table";
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

const EVENT_PAGE_SIZE = 20;

export default function MetagenResultPage() {
  const [eventOffset, setEventOffset] = useState(0);

  const tz = useDisplayTz();
  const { selection: sel, setSelection: setSel } = usePersistedRangeState(
    RANGE_KEYS.metagenResultEvents,
  );
  const range = useMemo(() => resolveRange(sel, "datetime", tz), [sel, tz]);

  // Confs drive the queue's conf_id filter select. Read a generous first page.
  const { data: confsData } = useMetagenConfList({ offset: 0, limit: 100 });

  const { data: events } = useMetagenEvents({
    from: range.from,
    to: range.to,
    offset: eventOffset,
    limit: EVENT_PAGE_SIZE,
  });

  useEffect(() => {
    setEventOffset(0);
  }, [sel]);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Review queue</h1>

      <section className="rounded-lg border p-5">
        <h2 className="mb-4 text-sm font-medium">item queue (cross-dataset, cross-conf)</h2>
        <QueueTable confs={confsData?.confs ?? []} />
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
            limit: EVENT_PAGE_SIZE,
            totalCount: events?.total_count ?? 0,
          }}
          onPrev={() => setEventOffset(Math.max(0, eventOffset - EVENT_PAGE_SIZE))}
          onNext={() => setEventOffset(eventOffset + EVENT_PAGE_SIZE)}
        />
      </section>
    </div>
  );
}

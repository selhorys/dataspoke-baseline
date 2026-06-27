/**
 * MetagenConfView — read-only view of a metagen conf, shown on
 * /metagen/conf/[id] until Edit. Renders the conf fields (is_enabled,
 * schedule_tier, result_limit, overwrite_pending, dataset_filter) as bordered
 * panels; schedule_tier links to its backing Airflow DAG (metagen-<tier>).
 */

import { Badge } from "@/components/ui/badge";
import { FieldPanel } from "@/components/forms/field-panel";
import { FormGrid } from "@/components/ui/form-grid";
import { DatasetFilterView } from "@/components/dataset-filter-view";
import { ScheduleTierLink, scheduleDagId } from "@/components/schedule-tier-link";
import type { MetagenConf } from "@/types/metagen";
import type { DatasetFilter } from "@/types/governance";

interface MetagenConfViewProps {
  conf: MetagenConf;
  datasetFilter: DatasetFilter;
}

export function MetagenConfView({ conf, datasetFilter }: MetagenConfViewProps) {
  return (
    <FormGrid>
      <FieldPanel label="is_enabled">
        <Badge variant={conf.is_enabled ? "default" : "secondary"} className="text-xs">
          {conf.is_enabled ? "enabled" : "disabled"}
        </Badge>
      </FieldPanel>

      <FieldPanel label="schedule_tier">
        <ScheduleTierLink
          tier={conf.schedule_tier ?? "manual"}
          dagId={scheduleDagId("metagen", conf.schedule_tier)}
        />
      </FieldPanel>

      <FieldPanel label="result_limit">
        <span className="tabular-nums">{conf.result_limit}</span>
      </FieldPanel>

      <FieldPanel label="overwrite_pending">
        {conf.overwrite_pending ? "yes" : "no"}
      </FieldPanel>

      <div className="sm:col-span-2">
        <DatasetFilterView value={datasetFilter} />
      </div>
    </FormGrid>
  );
}

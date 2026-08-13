/**
 * OntogenConfView — read-only view of the singleton ontogen conf, shown on
 * /ontogen/conf until Edit. Renders the conf fields (is_enabled, schedule_tier,
 * dataset_filter, default_run_prompt) as bordered panels; schedule_tier links to
 * its backing Airflow DAG (ontogen-<tier>) and default_run_prompt renders as a
 * preformatted block (em dash when empty).
 */

import { Badge } from "@/components/ui/badge";
import { FieldPanel } from "@/components/forms/field-panel";
import { FormGrid } from "@/components/ui/form-grid";
import { DatasetFilterView } from "@/components/dataset-filter-view";
import { ScheduleTierLink, scheduleDagId } from "@/components/schedule-tier-link";
import type { OntogenConf } from "@/types/ontogen";

interface OntogenConfViewProps {
  conf: OntogenConf;
  /** dataset_filter — a SQL WHERE clause string. */
  datasetFilter: string;
}

export function OntogenConfView({ conf, datasetFilter }: OntogenConfViewProps) {
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
          dagId={scheduleDagId("ontogen", conf.schedule_tier)}
        />
      </FieldPanel>

      <div className="sm:col-span-2">
        <DatasetFilterView value={datasetFilter} />
      </div>

      <FieldPanel label="default_run_prompt" className="sm:col-span-2">
        {conf.default_run_prompt ? (
          <pre className="overflow-x-auto whitespace-pre-wrap rounded-md border bg-muted/40 p-3 font-mono text-xs">
            {conf.default_run_prompt}
          </pre>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </FieldPanel>
    </FormGrid>
  );
}

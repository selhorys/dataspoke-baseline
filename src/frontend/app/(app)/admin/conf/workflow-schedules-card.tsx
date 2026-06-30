"use client";

import { useDagGroups, useSetDagGroupPaused } from "@/lib/api/admin";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Checkbox } from "@/components/ui/checkbox";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { DagGroup, DagGroupStatus } from "@/lib/api/types";

// Fixed group order + labels — verbatim from spec/API.md §/admin/dags. The page
// invents no rows: it renders exactly these groups in this order, looking up
// each group's status from the GET /admin/dags response.
const GROUPS: { group: DagGroup; label: string }[] = [
  { group: "datahub_sync", label: "DataHub hourly sync" },
  { group: "auth_role_sync", label: "Auth role sync" },
  { group: "ingestion_active", label: "Active ingestion" },
  { group: "ontogen", label: "Ontology generation" },
  { group: "metagen", label: "Metadata generation" },
  { group: "metrics", label: "Metrics" },
];

export function WorkflowSchedulesCard() {
  const { data, isLoading, isError, error } = useDagGroups();
  const { mutate: setPaused, isPending, variables } = useSetDagGroupPaused();

  const byGroup = new Map<DagGroup, DagGroupStatus>(
    (data?.groups ?? []).map((g) => [g.group, g]),
  );

  function handleToggle(group: DagGroup, nextChecked: boolean) {
    // Checkbox reads "Enabled": checked = unpaused. A toggle (incl. toggling out of
    // the indeterminate/mixed state) sends an explicit paused value to bring all
    // member DAGs of the group into line.
    const paused = nextChecked !== true;
    setPaused(
      { group, paused },
      {
        onError: (err) => {
          const description =
            err instanceof ApiError ? err.message : "An unexpected error occurred.";
          toast({ variant: "destructive", title: "Schedule update failed", description });
        },
      },
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Workflow schedules</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-4 text-xs text-muted-foreground">
          Pause or unpause the periodic Airflow DAG groups. A group is enabled
          (unpaused) only when all its member DAGs are running; a mixed (indeterminate)
          state means members disagree.
        </p>
        {isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-5 w-48" />
            <Skeleton className="h-5 w-48" />
            <Skeleton className="h-5 w-48" />
          </div>
        ) : isError ? (
          <p className="text-sm text-destructive">
            {error instanceof ApiError ? error.message : "Failed to load workflow schedules."}
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {GROUPS.map(({ group, label }) => {
              const status = byGroup.get(group);
              const checked = status
                ? status.mixed
                  ? "indeterminate"
                  : !status.paused
                : false;
              const rowPending = isPending && variables?.group === group;
              const id = `dag-group-${group}`;
              return (
                <label
                  key={group}
                  htmlFor={id}
                  className="flex cursor-pointer items-center gap-3"
                >
                  <Checkbox
                    id={id}
                    checked={checked}
                    disabled={!status || rowPending}
                    onCheckedChange={(v) => handleToggle(group, v === true)}
                  />
                  <span className="text-sm">{label}</span>
                </label>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

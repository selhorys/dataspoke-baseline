"use client";

/**
 * ScheduleTierLink — renders an ingestion source's schedule tier ("daily",
 * "weekly", …). The scheduled tiers (hourly/daily/weekly) link to their backing
 * Airflow DAG (`ingestion-active-<tier>`); "manual" and "custom" have no DAG and
 * render as plain text. Falls back to plain text when no Airflow URL is configured.
 */

import { ExternalLink } from "lucide-react";
import { getRuntimeConfig } from "@/lib/runtime-config";
import { scheduleTierLabel } from "@/lib/ingestion-mode-variant";
import { cn } from "@/lib/utils";

const LINKABLE_TIERS = new Set(["hourly", "daily", "weekly"]);

export function ScheduleTierLink({
  schedule,
  className,
}: {
  schedule: string | null | undefined;
  className?: string;
}) {
  const tier = scheduleTierLabel(schedule);
  const { airflowUrl } = getRuntimeConfig();

  if (!airflowUrl || !LINKABLE_TIERS.has(tier)) {
    return <span className={className}>{tier}</span>;
  }

  return (
    <a
      href={`${airflowUrl}/dags/ingestion-active-${tier}`}
      target="_blank"
      rel="noopener noreferrer"
      title={`Open ingestion-active-${tier} in Airflow`}
      className={cn("inline-flex items-center gap-1 hover:underline", className)}
    >
      {tier}
      <ExternalLink className="h-3 w-3" aria-hidden="true" />
    </a>
  );
}

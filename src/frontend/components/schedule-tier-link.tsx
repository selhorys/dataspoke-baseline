"use client";

/**
 * ScheduleTierLink — renders a schedule tier label ("daily", "weekly", "manual",
 * …). The scheduled tiers (hourly/daily/weekly) link to their backing Airflow DAG
 * via `dagId`; unscheduled values ("manual", "custom") pass `dagId = null` and
 * render as plain text. Falls back to plain text when no Airflow URL is configured.
 *
 * Used across ingestion, metagen, and ontogen — each feature passes its own
 * `dagId` computed with `scheduleDagId(prefix, tier)`.
 */

import { ExternalLink } from "lucide-react";
import { getRuntimeConfig } from "@/lib/runtime-config";
import { cn } from "@/lib/utils";

const LINKABLE_TIERS = new Set(["hourly", "daily", "weekly"]);

/**
 * Builds the Airflow DAG id for a schedule tier, mirroring src/workflows/registry.py
 * (`<prefix>-<tier>` for hourly/daily/weekly). Returns null for unscheduled tiers
 * ("manual"/"custom"/null), which have no backing DAG and render as plain text.
 */
export function scheduleDagId(
  prefix: string,
  tier: string | null | undefined,
): string | null {
  if (!tier || !LINKABLE_TIERS.has(tier)) return null;
  return `${prefix}-${tier}`;
}

export function ScheduleTierLink({
  tier,
  dagId,
  className,
}: {
  /** Display label (e.g. "daily", "manual"). */
  tier: string;
  /** Backing Airflow DAG id, or null/undefined for unscheduled tiers. */
  dagId: string | null | undefined;
  className?: string;
}) {
  const { airflowUrl } = getRuntimeConfig();

  if (!airflowUrl || !dagId) {
    return <span className={className}>{tier}</span>;
  }

  return (
    <a
      href={`${airflowUrl}/dags/${dagId}`}
      target="_blank"
      rel="noopener noreferrer"
      title={`Open ${dagId} in Airflow`}
      className={cn("inline-flex items-center gap-1 hover:underline", className)}
    >
      {tier}
      <ExternalLink className="h-3 w-3" aria-hidden="true" />
    </a>
  );
}

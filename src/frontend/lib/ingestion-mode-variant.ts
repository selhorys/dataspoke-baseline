/**
 * Pure helpers for ingestion-source display: mode → Badge variant/label, and
 * cron → schedule-tier label.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §List View.
 * Mode enum: src/shared/models/ingestion.py Mode.
 * Schedule tiers mirror CRON_TO_TIER in src/shared/models/ingestion.py.
 */

import type { IngestionFilterKey, IngestionMode } from "@/types/ingestion";

export type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

/**
 * Maps an ingestion mode to a Badge variant.
 *   ACTIVE_CUSTOM_MANAGED → "default"    (DataSpoke runs the extractor)
 *   DATAHUB_MANAGED       → "secondary"  (synced, read-only)
 *   PASSIVE               → "outline"    (tracked, not executed)
 */
export function modeBadgeVariant(mode: IngestionMode): BadgeVariant {
  switch (mode) {
    case "ACTIVE_CUSTOM_MANAGED":
      return "default";
    case "DATAHUB_MANAGED":
      return "secondary";
    case "PASSIVE":
      return "outline";
  }
}

/** Short human-readable mode label. */
export function modeLabel(mode: IngestionMode): string {
  switch (mode) {
    case "ACTIVE_CUSTOM_MANAGED":
      return "Active";
    case "DATAHUB_MANAGED":
      return "DataHub-managed";
    case "PASSIVE":
      return "Passive";
  }
}

/** Full explanatory description of a mode (used in helper notes). */
export function modeDescription(mode: IngestionMode): string {
  switch (mode) {
    case "ACTIVE_CUSTOM_MANAGED":
      return "DataSpoke's custom extractor runs this recipe on a schedule.";
    case "DATAHUB_MANAGED":
      return "Synced from DataHub, which is the source of truth — read-only here.";
    case "PASSIVE":
      return "Ingested outside DataHub/DataSpoke; DataSpoke only tracks its scope.";
  }
}

// ── Conf-list filter keys ──────────────────────────────────────────────────────────

/**
 * Maps a conf-list filter key to the `{ mode }` query pair on
 * GET /spoke/ingestion/sources. ALL applies no constraint; each other key
 * pins the `mode`. Internal DataHub CLI wrapper sources are hidden by the
 * backend, so DataHub-managed shows only regular sources.
 */
export function filterKeyToQuery(key: IngestionFilterKey): {
  mode?: IngestionMode;
} {
  switch (key) {
    case "ALL":
      return {};
    case "DATAHUB_MANAGED":
      return { mode: "DATAHUB_MANAGED" };
    case "ACTIVE_CUSTOM_MANAGED":
      return { mode: "ACTIVE_CUSTOM_MANAGED" };
    case "PASSIVE":
      return { mode: "PASSIVE" };
  }
}

/** Human-readable label for each conf-list filter option. */
export function filterKeyLabel(key: IngestionFilterKey): string {
  switch (key) {
    case "ALL":
      return "All";
    case "DATAHUB_MANAGED":
      return "DataHub-managed";
    case "ACTIVE_CUSTOM_MANAGED":
      return "Active";
    case "PASSIVE":
      return "Passive";
  }
}

/** Ordered list of conf-list filter keys for the dropdown. */
export const INGESTION_FILTER_KEYS: IngestionFilterKey[] = [
  "ALL",
  "DATAHUB_MANAGED",
  "ACTIVE_CUSTOM_MANAGED",
  "PASSIVE",
];

// ── Schedule tiers ───────────────────────────────────────────────────────────────

/**
 * Canonical cron → tier map, mirroring CRON_TO_TIER in
 * src/shared/models/ingestion.py. Multiple equivalent cron forms map to the
 * same logical tier.
 */
const CRON_TO_TIER: Record<string, "hourly" | "daily" | "weekly"> = {
  "0 * * * *": "hourly",
  "@hourly": "hourly",
  "0 0 * * *": "daily",
  "@daily": "daily",
  "@midnight": "daily",
  "0 0 * * 0": "weekly",
  "@weekly": "weekly",
};

export type ScheduleTier = "hourly" | "daily" | "weekly" | "manual" | "custom";

/**
 * Maps a cron expression to a schedule-tier label.
 *   null               → "manual"  (manual-only)
 *   recognised cron    → "hourly" | "daily" | "weekly"
 *   unrecognised cron  → "custom"  (a non-canonical schedule)
 */
export function scheduleTierLabel(schedule: string | null | undefined): ScheduleTier {
  if (schedule === null || schedule === undefined) return "manual";
  return CRON_TO_TIER[schedule.trim()] ?? "custom";
}

/** Canonical cron for a writable tier (the value persisted on create/edit). */
export const TIER_TO_CANONICAL_CRON: Record<"hourly" | "daily" | "weekly", string> = {
  hourly: "0 * * * *",
  daily: "0 0 * * *",
  weekly: "0 0 * * 0",
};

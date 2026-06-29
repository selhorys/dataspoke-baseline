"use client";

/**
 * DatahubDatasetLink — a deep-link into DataHub's dataset page for a given URN.
 *
 * Builds `${datahubUrl}/dataset/{encoded-urn}` from the runtime config and renders
 * an external link (mirroring ScheduleTierLink's affordance) only when `datahubUrl`
 * is configured. When it is unset the component renders `fallback` (nothing by
 * default, or an em-dash in table cells), mirroring the app-shell infra-link gating.
 *
 * Reused across the dataset tables (unmanaged / source / metagen-uncovered /
 * dataset list) and the per-dataset header.
 */

import { ExternalLink } from "lucide-react";
import type { ReactNode } from "react";
import { getRuntimeConfig } from "@/lib/runtime-config";
import { cn } from "@/lib/utils";

/** Builds the DataHub dataset URL, or null when no DataHub URL is configured. */
export function datahubDatasetUrl(urn: string): string | null {
  const { datahubUrl } = getRuntimeConfig();
  if (!datahubUrl) return null;
  return `${datahubUrl}/dataset/${encodeURIComponent(urn)}`;
}

export function DatahubDatasetLink({
  urn,
  label = "DataHub",
  className,
  fallback = null,
}: {
  urn: string;
  /** Link text (defaults to "DataHub"). */
  label?: string;
  className?: string;
  /** Rendered when no DataHub URL is configured (e.g. an em-dash in table cells). */
  fallback?: ReactNode;
}) {
  const href = datahubDatasetUrl(urn);

  if (!href) {
    return <>{fallback}</>;
  }

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      title={`Open ${urn} in DataHub`}
      className={cn(
        "inline-flex items-center gap-1 text-sm hover:underline",
        className,
      )}
    >
      {label}
      <ExternalLink className="h-3 w-3" aria-hidden="true" />
    </a>
  );
}

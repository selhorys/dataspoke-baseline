"use client";

/**
 * DatahubDatasetLink — a deep-link into DataHub's dataset page for a given URN.
 *
 * Builds `${datahubUrl}/dataset/{encoded-urn}` and renders an external link
 * (mirroring ScheduleTierLink's affordance) only when `datahubUrl` resolves
 * non-empty. When it is unset the component renders `fallback` (nothing by
 * default, or an em-dash in table cells), mirroring the app-shell infra-link gating.
 *
 * The DataHub URL resolves env-first, then `GET /spoke/common/peripheral-links`
 * (see useDisplayLinks). This component renders once per table row, and that
 * hook shares one stable query key across every instance, so a table of N rows
 * still issues a single request.
 *
 * Reused across the dataset tables (unmanaged / source / metagen-uncovered /
 * dataset list) and the per-dataset header.
 */

import { ExternalLink } from "lucide-react";
import type { ReactNode } from "react";
import { useDisplayLinks } from "@/lib/api/peripheral-links";
import { cn } from "@/lib/utils";

/**
 * Builds the DataHub dataset URL, or null when no DataHub URL is configured.
 *
 * Pure: callers pass the already-resolved, already-safety-checked base URL
 * (from `useDisplayLinks()`), so this helper stays usable outside React.
 */
export function datahubDatasetUrl(datahubUrl: string, urn: string): string | null {
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
  const { datahubUrl } = useDisplayLinks();
  const href = datahubDatasetUrl(datahubUrl, urn);

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

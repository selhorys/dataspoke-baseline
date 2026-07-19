"use client";

/**
 * EvidenceLink — renders the Evidence cell of an OntoGen result row. A row's
 * `run_id` doubles as its Langfuse session id, so the full adversarial-debate
 * transcript lives in Langfuse rather than in the row. When `run_id` and the
 * browser-reachable Langfuse host + project slug are all present, this renders an
 * external "Link" opening `{langfuseUrl}/project/{projectId}/sessions/{run_id}`
 * in a new tab. Otherwise (seeded rows with no run, or tracing disabled), it
 * renders an em dash.
 *
 * The Langfuse host and project slug resolve env-first, then
 * `GET /spoke/common/peripheral-links` (see useDisplayLinks). This component
 * renders once per result row, and that hook shares one stable query key across
 * every instance, so a table of N rows still issues a single request.
 */

import { ExternalLink } from "lucide-react";
import { useDisplayLinks } from "@/lib/api/peripheral-links";

interface EvidenceLinkProps {
  /** Creating run's id; equals the Langfuse session id. */
  runId: string | null;
}

export function EvidenceLink({ runId }: EvidenceLinkProps) {
  const { langfuseUrl, langfuseProjectId } = useDisplayLinks();

  if (!runId || !langfuseUrl || !langfuseProjectId) {
    return <span className="text-muted-foreground">—</span>;
  }

  const href = `${langfuseUrl.replace(/\/+$/, "")}/project/${encodeURIComponent(
    langfuseProjectId,
  )}/sessions/${encodeURIComponent(runId)}`;

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-primary hover:underline"
    >
      Link
      <ExternalLink className="h-3 w-3" aria-hidden="true" />
    </a>
  );
}

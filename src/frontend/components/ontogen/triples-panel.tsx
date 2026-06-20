"use client";

/**
 * TriplesPanel — uniform compact table of ontology triples with an approval
 * filter, a created-at sort control, gated reason-confirm review, a per-row
 * Langfuse-session evidence link, and the shared Pagination control.
 * GET /spoke/ontogen/result/triple
 *
 * Triple approve is gated: disabled with a hover hint when the subject node,
 * predicate edge, or object node is not yet status='approved'. This mirrors the
 * backend's 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING guard. The node/edge lookups
 * for gating are fetched in bulk (not user-paged).
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { ReviewRow } from "@/components/ontogen/review-row";
import { EvidenceLink } from "@/components/ontogen/evidence-link";
import { ApprovalFilter } from "@/components/ontogen/approval-filter";
import { SortControl, type OntogenSortMode } from "@/components/ontogen/sort-control";
import { useOntogenEdges, useOntogenNodes, useOntogenTriples } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";
import { tripleApprovalGate, buildNodesById, buildEdgesById } from "@/lib/ontogen-triple-gate";
import { filterByApproval, type ApprovalFilterMode } from "@/lib/ontogen-filter";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

/** Bulk fetch size for the node/edge gating lookups (not user-paged). */
const GATE_LOOKUP_LIMIT = 1000;

interface TriplesPanelProps {
  canWrite: boolean;
}

export function TriplesPanel({ canWrite }: TriplesPanelProps) {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [mode, setMode] = useState<ApprovalFilterMode>("all");
  const [sort, setSort] = useState<OntogenSortMode>("created_at_desc");
  const tz = useDisplayTz();

  // Fetch triples (paginated) plus ALL nodes and edges for gating lookup.
  const triplesQuery = useOntogenTriples({ offset, limit, sort });
  const nodesQuery = useOntogenNodes({ limit: GATE_LOOKUP_LIMIT });
  const edgesQuery = useOntogenEdges({ limit: GATE_LOOKUP_LIMIT });

  const total = triplesQuery.data?.total_count ?? 0;
  const triples = filterByApproval(triplesQuery.data?.triples ?? [], mode);

  const nodesById = buildNodesById(nodesQuery.data?.nodes ?? []);
  const edgesById = buildEdgesById(edgesQuery.data?.edges ?? []);

  const isLoading = triplesQuery.isLoading || nodesQuery.isLoading || edgesQuery.isLoading;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-end gap-3">
        <SortControl
          value={sort}
          onChange={(s) => {
            setSort(s);
            setOffset(0);
          }}
        />
        <ApprovalFilter
          value={mode}
          onChange={(m) => {
            setMode(m);
            setOffset(0);
          }}
        />
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : triplesQuery.error ? (
        <p className="text-sm text-destructive">
          Failed to load triples: {triplesQuery.error.message}
        </p>
      ) : triples.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">No ontology triples.</p>
      ) : (
        <Table className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead className="w-[18%] text-xs">Title</TableHead>
              <TableHead className="w-[30%] text-xs">Description</TableHead>
              <TableHead className="w-[9%] text-xs">Status</TableHead>
              <TableHead className="w-[9%] text-xs">Confidence</TableHead>
              <TableHead className="w-[13%] text-xs">Actions</TableHead>
              <TableHead className="w-[11%] text-xs">Created At</TableHead>
              <TableHead className="w-[10%] text-xs">Evidence</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {triples.map((triple) => {
              const subjectName =
                nodesById.get(triple.subject_node_id)?.name ?? triple.subject_node_id;
              const edgeLabel = edgesById.get(triple.edge_id)?.label ?? triple.edge_id;
              const objectName =
                nodesById.get(triple.object_node_id)?.name ?? triple.object_node_id;
              const { canApprove, blockingHint } = tripleApprovalGate(
                triple,
                nodesById,
                edgesById,
              );

              return (
                <TableRow key={triple.id} className="text-xs">
                  <TableCell
                    className="truncate py-2 font-mono"
                    title={`${subjectName} --${edgeLabel}--> ${objectName}`}
                  >
                    {subjectName}
                    <span className="mx-1 text-muted-foreground">--{edgeLabel}--&gt;</span>
                    {objectName}
                  </TableCell>
                  <TableCell className="py-2 text-muted-foreground">—</TableCell>
                  <TableCell className="py-2">
                    <Badge variant={ontogenStatusVariant(triple.status)}>
                      {ontogenStatusLabel(triple.status)}
                    </Badge>
                  </TableCell>
                  <TableCell className="py-2">
                    <span className="text-muted-foreground">
                      {triple.confidence_score.toFixed(2)}
                    </span>
                  </TableCell>
                  <TableCell className="py-2">
                    {canWrite && (
                      <ReviewRow
                        id={triple.id}
                        kind="triple"
                        status={triple.status}
                        disabled={!canApprove}
                        disabledHint={blockingHint || undefined}
                      />
                    )}
                  </TableCell>
                  <TableCell className="whitespace-nowrap py-2 text-muted-foreground">
                    {formatDateTime(triple.created_at, tz)}
                  </TableCell>
                  <TableCell className="py-2">
                    <EvidenceLink runId={triple.run_id} />
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}

      <Pagination
        offset={offset}
        limit={limit}
        total={total}
        onOffset={setOffset}
        onLimit={setLimit}
      />
    </div>
  );
}

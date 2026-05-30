"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useIngestionList } from "@/lib/api/ingestion";
import { ErrorState } from "@/components/ui/error-state";
import { useMe } from "@/lib/auth/use-me";
import { formatRelativeTime } from "@/lib/format-time";
import type { IngestionConfigResponse } from "@/types/ingestion";

const PAGE_SIZE = 20;

/** Derive a latest-event summary from config.status and updated_at for display. */
function latestEventCell(config: IngestionConfigResponse): React.ReactNode {
  // The list endpoint returns config-level status, not event-level status.
  // Show status badge + relative time of last update as the event summary proxy.
  const variant = config.status === "OK" ? "default" : config.status === "ERROR" ? "destructive" : "secondary";
  const label = config.status === "OK" ? "OK" : config.status;
  const relTime = formatRelativeTime(config.updated_at);
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="text-muted-foreground">{relTime}</span>
      <Badge variant={variant} className="text-xs">
        {label}
      </Badge>
    </span>
  );
}

// ── New-config dialog ──────────────────────────────────────────────────────────

interface NewConfDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function NewConfDialog({ open, onOpenChange }: NewConfDialogProps) {
  const router = useRouter();
  const [urn, setUrn] = useState("");

  const handleGo = () => {
    const trimmed = urn.trim();
    if (!trimmed) return;
    // Client-side navigation preserves the in-memory access token — a hard
    // reload would drop it and bounce through /login. The form will be in
    // create state for a URN with no existing config.
    router.push(`/ingestion/data/${encodeURIComponent(trimmed)}`);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle>New ingestion config</DialogTitle>
          <DialogDescription>
            Enter the DataHub dataset URN to create an ingestion config for.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2 py-2">
          <Label htmlFor="new-conf-urn">dataset_urn</Label>
          <Input
            id="new-conf-urn"
            value={urn}
            onChange={(e) => setUrn(e.target.value)}
            placeholder="urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.orders,PROD)"
            onKeyDown={(e) => {
              if (e.key === "Enter") handleGo();
            }}
          />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleGo} disabled={!urn.trim()}>
            Go to dataset
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────────

export default function IngestionListPage() {
  const { canWrite } = useMe();
  const [offset, setOffset] = useState(0);
  const [showNewDialog, setShowNewDialog] = useState(false);

  const { data, isLoading, error } = useIngestionList({ offset, limit: PAGE_SIZE });

  const totalPages = data ? Math.ceil(data.total_count / PAGE_SIZE) : 0;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Ingestion Control</h1>
        {canWrite && (
          <Button size="sm" onClick={() => setShowNewDialog(true)}>
            <Plus className="mr-1 h-4 w-4" />
            New Conf
          </Button>
        )}
      </div>

      {error && (
        <ErrorState message={`Failed to load ingestion configs: ${error.message}`} />
      )}

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>dataset_urn</TableHead>
              <TableHead>mode</TableHead>
              <TableHead>tier</TableHead>
              <TableHead>events</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading &&
              Array.from({ length: 5 }).map((_, i) => (
                <TableRow key={i}>
                  {Array.from({ length: 4 }).map((__, j) => (
                    <TableCell key={j}>
                      <Skeleton className="h-4 w-full" />
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            {!isLoading && data?.configs.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="py-8 text-center text-muted-foreground">
                  No ingestion configs found.
                </TableCell>
              </TableRow>
            )}
            {data?.configs.map((c) => (
              <TableRow key={c.id} className="cursor-pointer hover:bg-muted/50">
                <TableCell>
                  <Link
                    href={`/ingestion/data/${encodeURIComponent(c.dataset_urn)}`}
                    className="font-mono text-sm hover:underline"
                  >
                    {c.dataset_urn}
                  </Link>
                </TableCell>
                <TableCell>
                  <Badge variant="outline" className="text-xs">
                    {c.mode}
                  </Badge>
                </TableCell>
                <TableCell className="text-sm">
                  {c.mode === "passive" ? (
                    <span className="text-muted-foreground">—</span>
                  ) : (
                    c.schedule_tier ?? "on-demand"
                  )}
                </TableCell>
                <TableCell>{latestEventCell(c)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            Page {currentPage} of {totalPages} ({data?.total_count ?? 0} total)
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={!data || offset + PAGE_SIZE >= data.total_count}
            >
              Next
            </Button>
          </div>
        </div>
      )}

      <NewConfDialog open={showNewDialog} onOpenChange={setShowNewDialog} />
    </div>
  );
}

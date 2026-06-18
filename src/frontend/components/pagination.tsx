"use client";

/**
 * Pagination — the app-wide standard pagination control. Adopted by every
 * user-paged table. Renders a page-size selector (20 / 50 / 100), Prev/Next,
 * numbered pages (with ellipsis for long ranges), and an "M–N of T" label.
 *
 * Offset-based: the caller owns `offset`/`limit` state and applies the new
 * values via `onOffset`/`onLimit`. Changing the page size resets to the first
 * page (callers should also reset their own offset on size change; this control
 * emits `onOffset(0)` alongside `onLimit` to keep the two in sync).
 */

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export const PAGE_SIZE_OPTIONS = [20, 50, 100] as const;
export const DEFAULT_PAGE_SIZE = 20;

interface PaginationProps {
  offset: number;
  limit: number;
  total: number;
  onOffset: (offset: number) => void;
  onLimit: (limit: number) => void;
}

/**
 * Builds the page-number sequence with ellipsis markers. Always shows the
 * first and last page, the current page, and one neighbour on each side.
 * `"ellipsis-left"` / `"ellipsis-right"` are sentinel gap markers.
 */
export function buildPageItems(
  currentPage: number,
  totalPages: number,
): Array<number | "ellipsis-left" | "ellipsis-right"> {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }

  const items: Array<number | "ellipsis-left" | "ellipsis-right"> = [1];
  const start = Math.max(2, currentPage - 1);
  const end = Math.min(totalPages - 1, currentPage + 1);

  if (start > 2) items.push("ellipsis-left");
  for (let p = start; p <= end; p += 1) items.push(p);
  if (end < totalPages - 1) items.push("ellipsis-right");

  items.push(totalPages);
  return items;
}

export function Pagination({ offset, limit, total, onOffset, onLimit }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.min(totalPages, Math.floor(offset / limit) + 1);

  const firstShown = total === 0 ? 0 : offset + 1;
  const lastShown = Math.min(offset + limit, total);

  const pageItems = buildPageItems(currentPage, totalPages);

  function goToPage(page: number) {
    const clamped = Math.min(Math.max(1, page), totalPages);
    onOffset((clamped - 1) * limit);
  }

  function handleLimitChange(value: string) {
    const next = Number(value);
    onOffset(0);
    onLimit(next);
  }

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground">Rows per page</span>
        <Select value={String(limit)} onValueChange={handleLimitChange}>
          <SelectTrigger className="h-8 w-20" aria-label="Rows per page">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PAGE_SIZE_OPTIONS.map((size) => (
              <SelectItem key={size} value={String(size)}>
                {size}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-muted-foreground">
          {firstShown}–{lastShown} of {total}
        </span>
      </div>

      <div className="flex items-center gap-1">
        <Button
          variant="outline"
          size="sm"
          onClick={() => goToPage(currentPage - 1)}
          disabled={currentPage <= 1}
        >
          Previous
        </Button>

        {pageItems.map((item) =>
          item === "ellipsis-left" || item === "ellipsis-right" ? (
            <span key={item} className="px-2 text-muted-foreground" aria-hidden="true">
              …
            </span>
          ) : (
            <Button
              key={item}
              variant={item === currentPage ? "default" : "outline"}
              size="sm"
              className="min-w-9"
              aria-current={item === currentPage ? "page" : undefined}
              onClick={() => goToPage(item)}
            >
              {item}
            </Button>
          ),
        )}

        <Button
          variant="outline"
          size="sm"
          onClick={() => goToPage(currentPage + 1)}
          disabled={currentPage >= totalPages}
        >
          Next
        </Button>
      </div>
    </div>
  );
}

/**
 * PageHeader — the shared page-title row, unifying the page-title type scale
 * (`font-display text-2xl font-semibold tracking-tight`) across every top-level
 * page. An optional `backHref` renders an ArrowLeft link; an optional `actions`
 * slot holds right-aligned controls (e.g. a RangePicker or a button).
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Design system (Type scale — Page title).
 */

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

interface PageHeaderProps {
  title: ReactNode;
  /** When set, renders a back-link with an ArrowLeft icon to the left of the title. */
  backHref?: string;
  /** Accessible label for the back link (defaults to "Back"). */
  backLabel?: string;
  /** Right-aligned slot — buttons, RangePicker, etc. */
  actions?: ReactNode;
  /** Extra classes applied to the title element (e.g. `font-mono` for URNs). */
  titleClassName?: string;
}

export function PageHeader({
  title,
  backHref,
  backLabel = "Back",
  actions,
  titleClassName,
}: PageHeaderProps) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 flex-wrap items-center gap-3">
        {backHref && (
          <Link
            href={backHref}
            className="text-muted-foreground hover:text-foreground"
            aria-label={backLabel}
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
        )}
        <h1
          className={cn(
            "truncate font-display text-2xl font-semibold tracking-tight",
            titleClassName,
          )}
        >
          {title}
        </h1>
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

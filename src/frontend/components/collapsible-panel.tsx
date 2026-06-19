"use client";

/**
 * CollapsiblePanel — a titled, foldable section matching the app's existing
 * `rounded-lg border p-5` section style. The header is a button toggle with a
 * chevron; an optional `actions` slot (e.g. a RangePicker or status badge) sits
 * on the right and does not toggle the panel. State is local `useState`; pass
 * `defaultOpen` to seed it.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page.
 */

import { useId, useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

interface CollapsiblePanelProps {
  title: ReactNode;
  /** Right-aligned slot rendered in the header; clicks do not toggle the panel. */
  actions?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}

export function CollapsiblePanel({
  title,
  actions,
  defaultOpen = true,
  children,
}: CollapsiblePanelProps) {
  const [open, setOpen] = useState(defaultOpen);
  const contentId = useId();

  return (
    <section className="rounded-lg border">
      <div className="flex items-center justify-between gap-3 p-5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={contentId}
          className="flex flex-1 items-center gap-2 text-left text-sm font-medium"
        >
          <ChevronDown
            className={cn(
              "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
              open ? "" : "-rotate-90",
            )}
            aria-hidden="true"
          />
          {title}
        </button>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>
      {open && (
        <div id={contentId} className="px-5 pb-5">
          {children}
        </div>
      )}
    </section>
  );
}

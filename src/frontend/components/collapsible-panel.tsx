"use client";

/**
 * CollapsiblePanel — a titled, foldable section matching the app's existing
 * `rounded-lg border` section style. The header is a button toggle with a
 * chevron over a muted header bar (a divider appears when open); an optional
 * `actions` slot (e.g. a RangePicker or status badge) sits on the right and does
 * not toggle the panel. An optional `accent` paints a feature-hued left spine —
 * the hub-and-spoke signature. State is local `useState`; pass `defaultOpen` to
 * seed it.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Design system / §Per-dataset page.
 */

import { useId, useState, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

type FeatureAccent =
  | "ingestion"
  | "validation"
  | "ontogen"
  | "metagen"
  | "governance";

// Full class names so Tailwind's content scanner keeps them in the build.
const ACCENT_SPINE: Record<FeatureAccent, string> = {
  ingestion: "border-l-2 border-l-feature-ingestion",
  validation: "border-l-2 border-l-feature-validation",
  ontogen: "border-l-2 border-l-feature-ontogen",
  metagen: "border-l-2 border-l-feature-metagen",
  governance: "border-l-2 border-l-feature-governance",
};

interface CollapsiblePanelProps {
  title: ReactNode;
  /** Right-aligned slot rendered in the header; clicks do not toggle the panel. */
  actions?: ReactNode;
  /** Optional feature hue painted as a left spine; omit for a neutral panel. */
  accent?: FeatureAccent;
  defaultOpen?: boolean;
  children: ReactNode;
}

export function CollapsiblePanel({
  title,
  actions,
  accent,
  defaultOpen = true,
  children,
}: CollapsiblePanelProps) {
  const [open, setOpen] = useState(defaultOpen);
  const contentId = useId();

  return (
    <section
      className={cn(
        "overflow-hidden rounded-lg border",
        accent && ACCENT_SPINE[accent],
      )}
    >
      <div
        className={cn(
          "flex items-center justify-between gap-3 bg-muted/40 p-5",
          open && "border-b",
        )}
      >
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={contentId}
          className="flex flex-1 items-center gap-2 text-left font-display text-sm font-semibold text-foreground"
        >
          <ChevronDown
            className={cn(
              "h-4 w-4 shrink-0 transition-transform",
              open ? "text-brand" : "-rotate-90 text-muted-foreground",
            )}
            aria-hidden="true"
          />
          {title}
        </button>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>
      {open && (
        <div id={contentId} className="px-5 pb-5 pt-5">
          {children}
        </div>
      )}
    </section>
  );
}

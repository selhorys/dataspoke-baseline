"use client";

/**
 * NotificationCenter — bell icon popover aggregating recent events.
 *
 * Polls two global cross-feature event feeds that exist in the API:
 *   - GET /spoke/ontogen/event   (global ontogen run history)
 *   - GET /spoke/metagen/event   (global metagen run history)
 *
 * Governance events are per-metric only (no global feed endpoint exists).
 * The two polled feeds each fetch limit=10; events are merged client-side,
 * deduped by id, sorted by occurred_at DESC, and capped at 20 items.
 * A single shared 15-second poll interval is used for both feeds (via usePoll).
 *
 * Unread count is tracked in localStorage (key: "dataspoke:notification-last-read").
 * A row navigates to the feature page when a deep link is derivable.
 */

import { useMemo, useState, useEffect, useCallback } from "react";
import { Bell } from "lucide-react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useOntogenEvents } from "@/lib/api/ontogen";
import { useMetagenEvents } from "@/lib/api/metagen";
import { mergeAndCapEvents, deriveEventDeepLink } from "@/lib/notification-events";
import type { NotificationEvent, RawEventList } from "@/lib/notification-events";
import { formatRelativeTime } from "@/lib/format-time";
import { eventStatusVariant } from "@/lib/event-status-variant";

const STORAGE_KEY = "dataspoke:notification-last-read";
const MAX_EVENTS = 20;
const POLL_LIMIT = 10;

function loadLastReadAt(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function saveLastReadAt(iso: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, iso);
  } catch {
    // ignore
  }
}

// ── Individual notification row ───────────────────────────────────────────────

interface NotificationRowProps {
  event: NotificationEvent;
  onNavigate: (path: string) => void;
}

function NotificationRow({ event, onNavigate }: NotificationRowProps) {
  const deepLink = deriveEventDeepLink(event);
  const variant = eventStatusVariant(event.status);

  const inner = (
    <div
      className={
        "flex flex-col gap-0.5 rounded-sm px-2 py-2 text-sm" +
        (deepLink ? " cursor-pointer hover:bg-accent" : "")
      }
    >
      <div className="flex items-center gap-2">
        <span className="font-medium capitalize">{event.source}</span>
        <Badge variant={variant} className="text-xs">
          {event.status}
        </Badge>
        <span className="ml-auto text-xs text-muted-foreground">
          {formatRelativeTime(event.occurred_at)}
        </span>
      </div>
      <span className="text-xs text-muted-foreground">{event.event_type}</span>
    </div>
  );

  if (deepLink) {
    return (
      <div
        role="button"
        tabIndex={0}
        onClick={() => onNavigate(deepLink)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") onNavigate(deepLink);
        }}
        className="w-full text-left"
      >
        {inner}
      </div>
    );
  }

  return inner;
}

// ── NotificationCenter ────────────────────────────────────────────────────────

export function NotificationCenter() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [lastReadAt, setLastReadAt] = useState<string | null>(null);

  useEffect(() => {
    setLastReadAt(loadLastReadAt());
  }, []);

  const { data: ontogenData, isLoading: ontogenLoading } = useOntogenEvents(POLL_LIMIT);
  const { data: metaData, isLoading: metaLoading } = useMetagenEvents({ limit: POLL_LIMIT });

  const isLoading = ontogenLoading || metaLoading;

  const events: NotificationEvent[] = useMemo(() => {
    return mergeAndCapEvents(
      {
        ontogen: ontogenData as RawEventList | undefined,
        metagen: metaData as RawEventList | undefined,
      },
      MAX_EVENTS,
    );
  }, [ontogenData, metaData]);

  // Count events newer than lastReadAt
  const unreadCount = useMemo(() => {
    if (!lastReadAt) return events.length;
    return events.filter(
      (e) => new Date(e.occurred_at).getTime() > new Date(lastReadAt).getTime(),
    ).length;
  }, [events, lastReadAt]);

  const handleOpen = useCallback(
    (value: boolean) => {
      setOpen(value);
      if (value && events.length > 0) {
        const newest = events[0].occurred_at;
        setLastReadAt(newest);
        saveLastReadAt(newest);
      }
    },
    [events],
  );

  const handleNavigate = useCallback(
    (path: string) => {
      setOpen(false);
      router.push(path);
    },
    [router],
  );

  return (
    <DropdownMenu open={open} onOpenChange={handleOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="relative" aria-label="Notifications">
          <Bell className="h-4 w-4" />
          {unreadCount > 0 && (
            <span className="absolute right-1 top-1 flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-destructive px-0.5 text-[10px] font-semibold text-destructive-foreground">
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuLabel className="flex items-center justify-between">
          <span>Notifications</span>
          {events.length > 0 && (
            <span className="text-xs font-normal text-muted-foreground">
              {events.length} recent
            </span>
          )}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />

        <div className="max-h-80 overflow-y-auto">
          {isLoading && events.length === 0 && (
            <div className="space-y-2 p-2">
              {Array.from({ length: 3 }).map((_, i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          )}

          {!isLoading && events.length === 0 && (
            <p className="px-2 py-4 text-center text-sm text-muted-foreground">
              No recent events.
            </p>
          )}

          {events.map((event) => (
            <NotificationRow
              key={event.id}
              event={event}
              onNavigate={handleNavigate}
            />
          ))}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

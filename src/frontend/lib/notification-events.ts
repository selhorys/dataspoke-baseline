/**
 * Pure utility functions for the NotificationCenter.
 * No React imports — safe to use in both client and test contexts.
 */

export interface NotificationEvent {
  id: string;
  source: string; // "ontogen" | "metagen"
  entity_type: string;
  entity_id: string;
  event_type: string;
  status: string;
  occurred_at: string;
  detail: Record<string, unknown>;
}

export interface RawEventItem {
  id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  status: string;
  occurred_at: string;
  detail: Record<string, unknown>;
}

export interface RawEventList {
  events: RawEventItem[];
}

/**
 * Merges event lists from multiple feature sources, deduplicates by id,
 * sorts by occurred_at descending, and caps the result at `maxItems`.
 *
 * @param sources - Map of source name to RawEventList (may be undefined/null)
 * @param maxItems - Maximum number of events to return (default 20)
 */
export function mergeAndCapEvents(
  sources: Record<string, RawEventList | null | undefined>,
  maxItems = 20,
): NotificationEvent[] {
  const seen = new Set<string>();
  const merged: NotificationEvent[] = [];

  for (const [source, list] of Object.entries(sources)) {
    if (!list) continue;
    for (const e of list.events) {
      if (!seen.has(e.id)) {
        seen.add(e.id);
        merged.push({ ...e, source });
      }
    }
  }

  merged.sort((a, b) => {
    const ta = new Date(a.occurred_at).getTime();
    const tb = new Date(b.occurred_at).getTime();
    return tb - ta;
  });

  return merged.slice(0, maxItems);
}

/**
 * Derives a deep-link UI path from a notification event.
 * Returns a path string when a deep link can be inferred,
 * or null when the event is display-only.
 */
export function deriveEventDeepLink(event: NotificationEvent): string | null {
  const { source, event_type } = event;

  // OntoGen: run events link to the main ontogen page
  if (source === "ontogen") {
    if (event_type.includes("NODE")) return "/ontogen";
    if (event_type.includes("EDGE")) return "/ontogen";
    if (event_type.includes("TRIPLE")) return "/ontogen";
    return "/ontogen";
  }

  // MetaGen: run events link to the main metagen page
  if (source === "metagen") {
    return "/metagen";
  }

  return null;
}

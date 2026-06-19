/**
 * Unified per-dataset types — derived from src/api/schemas/events.py
 * (EventResponse / EventListResponse) and the unified dataset timeline at
 * GET /spoke/common/data/{urn}/event.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page, spec/API.md §Common.
 */

/**
 * Public major-type filter values for the unified per-dataset event timeline.
 * Each maps to an event-type prefix server-side (INGESTION/VALIDATION/METAGEN).
 */
export type EventMajorType = "INGESTION" | "VALIDATION" | "METAGEN";

export const EVENT_MAJOR_TYPES: readonly EventMajorType[] = [
  "INGESTION",
  "VALIDATION",
  "METAGEN",
] as const;

/**
 * One row of the unified dataset timeline. The timeline unions the covering
 * source's ingestion runs with the dataset's validation and metagen events;
 * `wrapper` is true when the row was booked on an internal DataHub CLI wrapper
 * source linked to the covering source.
 */
export interface DatasetEvent {
  id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  status: string;
  detail: Record<string, unknown>;
  occurred_at: string;
  wrapper?: boolean;
}

export interface DatasetEventListResponse {
  offset: number;
  limit: number;
  total_count: number;
  events: DatasetEvent[];
}

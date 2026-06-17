/**
 * Tests for lib/notification-events.ts — mergeAndCapEvents + deriveEventDeepLink.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Shared Component Notes §NotificationCenter:
 *     bell-icon popover that polls per-feature event/... endpoints.
 *   - spec/feature/FRONTEND_BASIC.md §Live Updates:
 *     polling-only; no streaming surface; refetchInterval against event/... endpoints.
 *   - src/api/schemas/events.py EventResponse:
 *     {id, entity_type, entity_id, event_type, status, detail, occurred_at}
 *   - mergeAndCapEvents aggregation contract:
 *     dedup by id, sort occurred_at DESC, cap at maxItems (default 20).
 *   - deriveEventDeepLink routing contract:
 *     source="ontogen" result-row events (NODE/EDGE/TRIPLE) → "/ontogen/result",
 *     other ontogen events → "/ontogen"; source="metagen" → "/metagen";
 *     unknown/unmappable → null.
 */

import { describe, it, expect } from "vitest";
import { mergeAndCapEvents, deriveEventDeepLink } from "./notification-events";
import type { RawEventList, NotificationEvent } from "./notification-events";

// ---------------------------------------------------------------------------
// Factory helpers — minimal RawEventItem shapes matching EventResponse SSOT
// (src/api/schemas/events.py)
// ---------------------------------------------------------------------------

function makeEvent(
  id: string,
  occurred_at: string,
  overrides: Partial<{
    entity_type: string;
    entity_id: string;
    event_type: string;
    status: string;
    detail: Record<string, unknown>;
  }> = {},
) {
  return {
    id,
    entity_type: overrides.entity_type ?? "ontogen_run",
    entity_id: overrides.entity_id ?? "run-" + id,
    event_type: overrides.event_type ?? "NODE_DISCOVERED",
    status: overrides.status ?? "success",
    occurred_at,
    detail: overrides.detail ?? {},
  };
}

function makeList(events: ReturnType<typeof makeEvent>[]): RawEventList {
  return { events };
}

// ---------------------------------------------------------------------------
// 1. mergeAndCapEvents — deduplication by id
// ---------------------------------------------------------------------------

describe("mergeAndCapEvents — deduplication by id (same id from two sources appears once)", () => {
  // Contract: same event id present in multiple sources must appear exactly once in output.
  // spec/feature/FRONTEND_BASIC.md §NotificationCenter — polls multiple feature feeds.

  it("same id from two different sources appears exactly once", () => {
    const sharedEvent = makeEvent("evt-shared", "2026-05-01T10:00:00Z");
    const result = mergeAndCapEvents({
      ontogen: makeList([sharedEvent]),
      metagen: makeList([sharedEvent]),
    });

    const ids = result.map((e) => e.id);
    expect(ids.filter((id) => id === "evt-shared")).toHaveLength(1);
  });

  it("distinct ids from two sources both appear in output", () => {
    const result = mergeAndCapEvents({
      ontogen: makeList([makeEvent("evt-1", "2026-05-01T10:00:00Z")]),
      metagen: makeList([makeEvent("evt-2", "2026-05-01T09:00:00Z")]),
    });

    const ids = result.map((e) => e.id);
    expect(ids).toContain("evt-1");
    expect(ids).toContain("evt-2");
    expect(ids).toHaveLength(2);
  });

  it("duplicate id in same source list is also deduped", () => {
    // RawEventList from the API should never contain duplicates, but the
    // merge function must be defensive against it.
    const result = mergeAndCapEvents({
      ontogen: makeList([
        makeEvent("evt-dup", "2026-05-01T10:00:00Z"),
        makeEvent("evt-dup", "2026-05-01T10:00:00Z"),
      ]),
    });

    const ids = result.map((e) => e.id);
    expect(ids.filter((id) => id === "evt-dup")).toHaveLength(1);
  });

  it("three sources with one shared id → shared id appears once, unique ids all appear", () => {
    const shared = makeEvent("shared", "2026-05-01T12:00:00Z");
    const result = mergeAndCapEvents({
      ontogen: makeList([shared, makeEvent("uniq-a", "2026-05-01T11:00:00Z")]),
      metagen: makeList([shared, makeEvent("uniq-b", "2026-05-01T10:00:00Z")]),
      other: makeList([shared]),
    });

    const ids = result.map((e) => e.id);
    expect(ids.filter((id) => id === "shared")).toHaveLength(1);
    expect(ids).toContain("uniq-a");
    expect(ids).toContain("uniq-b");
    expect(ids).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------
// 2. mergeAndCapEvents — sort by occurred_at DESCENDING
// ---------------------------------------------------------------------------

describe("mergeAndCapEvents — sort by occurred_at DESC (newest first)", () => {
  // Contract: output is sorted newest-first by occurred_at ISO timestamp.
  // Distinct ISO timestamps are required to assert strict ordering.

  it("single source with out-of-order events is sorted newest-first", () => {
    const result = mergeAndCapEvents({
      ontogen: makeList([
        makeEvent("old", "2026-01-15T08:00:00Z"),
        makeEvent("newest", "2026-05-20T14:30:00Z"),
        makeEvent("mid", "2026-03-10T09:15:00Z"),
      ]),
    });

    expect(result[0].id).toBe("newest");
    expect(result[1].id).toBe("mid");
    expect(result[2].id).toBe("old");
  });

  it("events from two sources are interleaved in descending order", () => {
    const result = mergeAndCapEvents({
      ontogen: makeList([
        makeEvent("onto-1", "2026-05-01T12:00:00Z"),
        makeEvent("onto-2", "2026-05-01T08:00:00Z"),
      ]),
      metagen: makeList([
        makeEvent("meta-1", "2026-05-01T10:00:00Z"),
        makeEvent("meta-2", "2026-05-01T06:00:00Z"),
      ]),
    });

    expect(result[0].id).toBe("onto-1"); // 12:00
    expect(result[1].id).toBe("meta-1"); // 10:00
    expect(result[2].id).toBe("onto-2"); // 08:00
    expect(result[3].id).toBe("meta-2"); // 06:00
  });

  it("occurred_at ordering is strictly DESC across full output", () => {
    const result = mergeAndCapEvents({
      ontogen: makeList([
        makeEvent("e3", "2026-06-03T00:00:00Z"),
        makeEvent("e1", "2026-06-01T00:00:00Z"),
      ]),
      metagen: makeList([
        makeEvent("e4", "2026-06-04T00:00:00Z"),
        makeEvent("e2", "2026-06-02T00:00:00Z"),
      ]),
    });

    for (let i = 0; i < result.length - 1; i++) {
      const curr = new Date(result[i].occurred_at).getTime();
      const next = new Date(result[i + 1].occurred_at).getTime();
      expect(curr).toBeGreaterThanOrEqual(next);
    }
  });
});

// ---------------------------------------------------------------------------
// 3. mergeAndCapEvents — cap at maxItems (default 20, cap after sort)
// ---------------------------------------------------------------------------

describe("mergeAndCapEvents — caps at maxItems after sort (default 20)", () => {
  // Contract: cap is applied AFTER sort — so the capped output contains the
  // NEWEST maxItems events, not an arbitrary subset.
  // spec/feature/FRONTEND_BASIC.md §NotificationCenter — bell popover is bounded.

  it("25 events → exactly 20 returned by default (default maxItems=20)", () => {
    const events = Array.from({ length: 25 }, (_, i) => {
      // Pad index so timestamps are distinct and sortable: newest = index 24
      const ts = `2026-05-${String(i + 1).padStart(2, "0")}T00:00:00Z`;
      return makeEvent(`evt-${i}`, ts);
    });

    const result = mergeAndCapEvents({ feed: makeList(events) });
    expect(result).toHaveLength(20);
  });

  it("cap is applied AFTER sort — the 20 returned are the newest 20, not the first 20 inserted", () => {
    // Build 25 events with distinct timestamps; newest is index 24.
    const events = Array.from({ length: 25 }, (_, i) => {
      const ts = `2026-05-${String(i + 1).padStart(2, "0")}T00:00:00Z`;
      return makeEvent(`evt-${i}`, ts);
    });

    const result = mergeAndCapEvents({ feed: makeList(events) });

    // After sort DESC, indices 24..5 are the top 20.
    // evt-24 (newest) must be in result; evt-0 (oldest) must NOT be.
    const ids = result.map((e) => e.id);
    expect(ids).toContain("evt-24");
    expect(ids).not.toContain("evt-0");
    expect(ids).not.toContain("evt-1");
    expect(ids).not.toContain("evt-2");
    expect(ids).not.toContain("evt-3");
    expect(ids).not.toContain("evt-4");
  });

  it("fewer than maxItems events → all returned, no padding", () => {
    const result = mergeAndCapEvents({
      feed: makeList([
        makeEvent("e1", "2026-05-01T00:00:00Z"),
        makeEvent("e2", "2026-05-02T00:00:00Z"),
      ]),
    });

    expect(result).toHaveLength(2);
  });

  it("explicit maxItems=5 → returns exactly 5 from 10 events", () => {
    const events = Array.from({ length: 10 }, (_, i) =>
      makeEvent(`evt-${i}`, `2026-05-${String(i + 1).padStart(2, "0")}T00:00:00Z`),
    );

    const result = mergeAndCapEvents({ feed: makeList(events) }, 5);
    expect(result).toHaveLength(5);
  });

  it("explicit maxItems=5 with 10 events → returns the 5 NEWEST", () => {
    const events = Array.from({ length: 10 }, (_, i) =>
      makeEvent(`evt-${i}`, `2026-05-${String(i + 1).padStart(2, "0")}T00:00:00Z`),
    );
    // Newest are indices 9..5 (days 10..6)

    const result = mergeAndCapEvents({ feed: makeList(events) }, 5);
    const ids = result.map((e) => e.id);
    expect(ids).toContain("evt-9");
    expect(ids).toContain("evt-8");
    expect(ids).toContain("evt-7");
    expect(ids).toContain("evt-6");
    expect(ids).toContain("evt-5");
    expect(ids).not.toContain("evt-0");
    expect(ids).not.toContain("evt-4");
  });

  it("maxItems=1 → returns exactly 1 (the newest)", () => {
    const result = mergeAndCapEvents(
      {
        feed: makeList([
          makeEvent("e1", "2026-01-01T00:00:00Z"),
          makeEvent("e2", "2026-06-01T00:00:00Z"),
        ]),
      },
      1,
    );

    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("e2");
  });
});

// ---------------------------------------------------------------------------
// 4. mergeAndCapEvents — null / undefined / empty sources
// ---------------------------------------------------------------------------

describe("mergeAndCapEvents — null/undefined/empty sources return [] without throwing", () => {
  // Contract: sources may be null, undefined, or empty; function must not throw.
  // spec/feature/FRONTEND_BASIC.md §Live Updates: polling may return empty/null feeds.

  it("{a: null, b: undefined, c: []} yields empty array without throwing", () => {
    const list: RawEventList = { events: [] };
    expect(() =>
      mergeAndCapEvents({ a: null, b: undefined, c: list }),
    ).not.toThrow();

    const result = mergeAndCapEvents({ a: null, b: undefined, c: list });
    expect(result).toEqual([]);
  });

  it("all-null sources → empty array", () => {
    expect(mergeAndCapEvents({ x: null, y: null })).toEqual([]);
  });

  it("all-undefined sources → empty array", () => {
    expect(mergeAndCapEvents({ x: undefined, y: undefined })).toEqual([]);
  });

  it("empty sources object → empty array", () => {
    expect(mergeAndCapEvents({})).toEqual([]);
  });

  it("null source mixed with populated source → only populated events returned", () => {
    const result = mergeAndCapEvents({
      ontogen: null,
      metagen: makeList([makeEvent("live", "2026-05-01T00:00:00Z")]),
    });

    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("live");
  });

  it("undefined source mixed with populated source → only populated events returned", () => {
    const result = mergeAndCapEvents({
      ontogen: undefined,
      metagen: makeList([makeEvent("live2", "2026-05-01T00:00:00Z")]),
    });

    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("live2");
  });
});

// ---------------------------------------------------------------------------
// 5. mergeAndCapEvents — source label is preserved on merged items
// ---------------------------------------------------------------------------

describe("mergeAndCapEvents — merged item carries source label (feature origin)", () => {
  // Contract: each NotificationEvent.source identifies which feed it came from.
  // The component uses source to route deep links and label notifications.

  it("event from ontogen source carries source='ontogen'", () => {
    const result = mergeAndCapEvents({
      ontogen: makeList([makeEvent("evt-onto", "2026-05-01T00:00:00Z")]),
    });

    expect(result[0].source).toBe("ontogen");
  });

  it("event from metagen source carries source='metagen'", () => {
    const result = mergeAndCapEvents({
      metagen: makeList([makeEvent("evt-meta", "2026-05-01T00:00:00Z")]),
    });

    expect(result[0].source).toBe("metagen");
  });

  it("events from different sources carry their respective source labels after merge", () => {
    const result = mergeAndCapEvents({
      ontogen: makeList([makeEvent("onto-1", "2026-05-02T00:00:00Z")]),
      metagen: makeList([makeEvent("meta-1", "2026-05-01T00:00:00Z")]),
    });

    const ontoItem = result.find((e) => e.id === "onto-1");
    const metaItem = result.find((e) => e.id === "meta-1");

    expect(ontoItem?.source).toBe("ontogen");
    expect(metaItem?.source).toBe("metagen");
  });

  it("when same id present in two sources, source label is the FIRST source that won dedup", () => {
    // The dedup keeps the first occurrence encountered during iteration.
    // The exact winning source depends on object-key iteration order — we
    // assert the source is one of the two valid values (not fabricated).
    const shared = makeEvent("shared-id", "2026-05-01T00:00:00Z");
    const result = mergeAndCapEvents({
      ontogen: makeList([shared]),
      metagen: makeList([shared]),
    });

    expect(["ontogen", "metagen"]).toContain(result[0].source);
    expect(result).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 6. deriveEventDeepLink — ontogen events → /ontogen
// ---------------------------------------------------------------------------

describe("deriveEventDeepLink — ontogen source routing (spec/feature/FRONTEND_BASIC.md §Routing)", () => {
  // Contract: ontogen result-row events (NODE/EDGE/TRIPLE) link to the result
  // browser at /ontogen/result; all other ontogen events fall back to /ontogen.
  // spec/feature/FRONTEND_BASIC.md §Routing: /ontogen → Ontology Generation.

  function ontoEvent(event_type: string): NotificationEvent {
    return {
      id: "evt-onto",
      source: "ontogen",
      entity_type: "ontogen_run",
      entity_id: "run-1",
      event_type,
      status: "success",
      occurred_at: "2026-05-01T00:00:00Z",
      detail: {},
    };
  }

  it("ontogen NODE event → /ontogen/result (result-row event → result browser)", () => {
    expect(deriveEventDeepLink(ontoEvent("NODE_DISCOVERED"))).toBe("/ontogen/result");
  });

  it("ontogen EDGE event → /ontogen/result (result-row event → result browser)", () => {
    expect(deriveEventDeepLink(ontoEvent("EDGE_DISCOVERED"))).toBe("/ontogen/result");
  });

  it("ontogen TRIPLE event → /ontogen/result (result-row event → result browser)", () => {
    expect(deriveEventDeepLink(ontoEvent("TRIPLE_APPROVED"))).toBe("/ontogen/result");
  });

  it("ontogen event with generic event_type → /ontogen (non-result-row events fall back to the main ontogen page)", () => {
    expect(deriveEventDeepLink(ontoEvent("run_completed"))).toBe("/ontogen");
  });

  it("ontogen deep link is a valid in-app route (starts with /)", () => {
    const link = deriveEventDeepLink(ontoEvent("NODE_DISCOVERED"));
    expect(link).not.toBeNull();
    expect(link!.startsWith("/")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 7. deriveEventDeepLink — metagen events → /metagen
// ---------------------------------------------------------------------------

describe("deriveEventDeepLink — metagen source → /metagen (spec/feature/FRONTEND_BASIC.md §Routing)", () => {
  // Contract: metagen events link to the metagen function page.
  // spec/feature/FRONTEND_BASIC.md §Routing: /metagen → Metadata Generation.

  function metaEvent(event_type: string): NotificationEvent {
    return {
      id: "evt-meta",
      source: "metagen",
      entity_type: "metagen_run",
      entity_id: "run-2",
      event_type,
      status: "success",
      occurred_at: "2026-05-01T00:00:00Z",
      detail: {},
    };
  }

  it("metagen run_completed event → /metagen", () => {
    expect(deriveEventDeepLink(metaEvent("run_completed"))).toBe("/metagen");
  });

  it("metagen run_started event → /metagen", () => {
    expect(deriveEventDeepLink(metaEvent("run_started"))).toBe("/metagen");
  });

  it("metagen deep link is a valid in-app route (starts with /)", () => {
    const link = deriveEventDeepLink(metaEvent("run_completed"));
    expect(link).not.toBeNull();
    expect(link!.startsWith("/")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 8. deriveEventDeepLink — URN-bearing links must be URL-safe (no raw colons)
// ---------------------------------------------------------------------------

describe("deriveEventDeepLink — returned path must not embed raw URN colons or commas", () => {
  // Contract: if the link targets a per-dataset page and embeds a URN segment,
  // it must encodeURIComponent the URN. Raw colons/commas in an href break
  // browser navigation and URL parsing.
  // Note: the current impl returns fixed routes (/ontogen, /metagen) — no URN
  // embedding. This test asserts that neither existing route contains raw URN
  // syntax (regression guard if future impl adds dataset-level deep links).

  it("ontogen deep link contains no raw colon that would break URL parsing", () => {
    const link = deriveEventDeepLink({
      id: "e1",
      source: "ontogen",
      entity_type: "dataset",
      entity_id: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
      event_type: "NODE_DISCOVERED",
      status: "success",
      occurred_at: "2026-05-01T00:00:00Z",
      detail: {},
    });

    // NODE_DISCOVERED is a result-row event → fixed "/ontogen/result", which is
    // safe; assert the URN is not embedded raw in the path.
    if (link !== null && link.includes("urn:li")) {
      // If any future impl embeds the URN, it must be encoded.
      expect(link).not.toMatch(/urn:li:/);
    } else {
      // Safe fixed path — no encoding issue.
      expect(link).toBe("/ontogen/result");
    }
  });

  it("metagen deep link contains no raw colon from entity_id URN", () => {
    const link = deriveEventDeepLink({
      id: "e2",
      source: "metagen",
      entity_type: "dataset",
      entity_id: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)",
      event_type: "run_completed",
      status: "success",
      occurred_at: "2026-05-01T00:00:00Z",
      detail: {},
    });

    if (link !== null && link.includes("urn:li")) {
      expect(link).not.toMatch(/urn:li:/);
    } else {
      expect(link).toBe("/metagen");
    }
  });
});

// ---------------------------------------------------------------------------
// 9. deriveEventDeepLink — unknown/unmappable source returns null
// ---------------------------------------------------------------------------

describe("deriveEventDeepLink — unknown source → null (no broken href, no throw)", () => {
  // Contract: unknown sources return null — caller renders as non-linked notification.
  // Governance was removed from the source routing; a governance-shaped event
  // must return null rather than producing a stale fabricated link.

  function unknownEvent(source: string, event_type = "run_completed"): NotificationEvent {
    return {
      id: "evt-unknown",
      source,
      entity_type: "unknown_entity",
      entity_id: "entity-1",
      event_type,
      status: "success",
      occurred_at: "2026-05-01T00:00:00Z",
      detail: {},
    };
  }

  it("unknown source 'ingestion' → null", () => {
    expect(deriveEventDeepLink(unknownEvent("ingestion"))).toBeNull();
  });

  it("unknown source 'validation' → null", () => {
    expect(deriveEventDeepLink(unknownEvent("validation"))).toBeNull();
  });

  it("governance-shaped source event → null (governance removed from notification routing)", () => {
    // Spec ref: the governance branch was removed — assert no fabricated link.
    expect(deriveEventDeepLink(unknownEvent("governance"))).toBeNull();
  });

  it("empty string source → null (no throw)", () => {
    expect(() => deriveEventDeepLink(unknownEvent(""))).not.toThrow();
    expect(deriveEventDeepLink(unknownEvent(""))).toBeNull();
  });

  it("arbitrary unknown source 'foobar' → null (no throw)", () => {
    expect(() => deriveEventDeepLink(unknownEvent("foobar"))).not.toThrow();
    expect(deriveEventDeepLink(unknownEvent("foobar"))).toBeNull();
  });

  it("returning null does not produce an empty string (empty string would create a broken href)", () => {
    const link = deriveEventDeepLink(unknownEvent("governance"));
    // null is the correct signal for non-linkable. Empty string would be a broken href.
    expect(link).not.toBe("");
  });
});

// ---------------------------------------------------------------------------
// 10. deriveEventDeepLink — returned paths match registered routes
// ---------------------------------------------------------------------------

describe("deriveEventDeepLink — returned paths match FRONTEND_BASIC.md §Routing table", () => {
  // The routes in FRONTEND_BASIC.md §Routing define the valid in-app paths.
  // Spec: /ontogen → Ontology Generation (result-row events deep-link to its
  // result browser at /ontogen/result), /metagen → Metadata Generation.
  // Any deep link returned by this function must be a registered route.

  const registeredRoutes = new Set(["/ontogen", "/ontogen/result", "/metagen"]);

  it("ontogen link is in the registered route set", () => {
    const link = deriveEventDeepLink({
      id: "e",
      source: "ontogen",
      entity_type: "t",
      entity_id: "eid",
      event_type: "NODE_DISCOVERED",
      status: "success",
      occurred_at: "2026-05-01T00:00:00Z",
      detail: {},
    });
    expect(link).not.toBeNull();
    expect(registeredRoutes.has(link!)).toBe(true);
  });

  it("metagen link is in the registered route set", () => {
    const link = deriveEventDeepLink({
      id: "e",
      source: "metagen",
      entity_type: "t",
      entity_id: "eid",
      event_type: "run_completed",
      status: "success",
      occurred_at: "2026-05-01T00:00:00Z",
      detail: {},
    });
    expect(link).not.toBeNull();
    expect(registeredRoutes.has(link!)).toBe(true);
  });
});

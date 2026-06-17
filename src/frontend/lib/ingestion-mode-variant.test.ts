import { describe, it, expect } from "vitest";
import {
  modeBadgeVariant,
  modeLabel,
  modeDescription,
  scheduleTierLabel,
  TIER_TO_CANONICAL_CRON,
  filterKeyToQuery,
  filterKeyLabel,
  INGESTION_FILTER_KEYS,
} from "./ingestion-mode-variant";
import type { IngestionFilterKey } from "@/types/ingestion";

describe("modeBadgeVariant", () => {
  it("maps each mode to a distinct variant", () => {
    expect(modeBadgeVariant("ACTIVE_CUSTOM_MANAGED")).toBe("default");
    expect(modeBadgeVariant("DATAHUB_MANAGED")).toBe("secondary");
    expect(modeBadgeVariant("PASSIVE")).toBe("outline");
  });
});

describe("modeLabel / modeDescription", () => {
  it("returns non-empty labels and descriptions for all modes", () => {
    for (const mode of [
      "ACTIVE_CUSTOM_MANAGED",
      "DATAHUB_MANAGED",
      "PASSIVE",
    ] as const) {
      expect(modeLabel(mode).length).toBeGreaterThan(0);
      expect(modeDescription(mode).length).toBeGreaterThan(0);
    }
  });
});

describe("scheduleTierLabel", () => {
  it("maps null to manual", () => {
    expect(scheduleTierLabel(null)).toBe("manual");
    expect(scheduleTierLabel(undefined)).toBe("manual");
  });

  it("maps canonical hourly crons", () => {
    expect(scheduleTierLabel("0 * * * *")).toBe("hourly");
    expect(scheduleTierLabel("@hourly")).toBe("hourly");
  });

  it("maps canonical daily crons (including @midnight)", () => {
    expect(scheduleTierLabel("0 0 * * *")).toBe("daily");
    expect(scheduleTierLabel("@daily")).toBe("daily");
    expect(scheduleTierLabel("@midnight")).toBe("daily");
  });

  it("maps canonical weekly crons", () => {
    expect(scheduleTierLabel("0 0 * * 0")).toBe("weekly");
    expect(scheduleTierLabel("@weekly")).toBe("weekly");
  });

  it("trims whitespace before matching", () => {
    expect(scheduleTierLabel("  0 0 * * *  ")).toBe("daily");
  });

  it("returns custom for an unrecognised cron", () => {
    expect(scheduleTierLabel("15 3 * * 1")).toBe("custom");
  });
});

describe("TIER_TO_CANONICAL_CRON", () => {
  it("round-trips canonical crons back to their tier", () => {
    expect(scheduleTierLabel(TIER_TO_CANONICAL_CRON.hourly)).toBe("hourly");
    expect(scheduleTierLabel(TIER_TO_CANONICAL_CRON.daily)).toBe("daily");
    expect(scheduleTierLabel(TIER_TO_CANONICAL_CRON.weekly)).toBe("weekly");
  });
});

// ── Conf-list filter keys ──────────────────────────────────────────────────────────
// Spec: spec/feature/FRONTEND_INGESTION.md §List View — the 5-option filter maps each
// key to the {mode?, adHoc?} query pair on GET /spoke/ingestion/sources. The two
// DataHub-managed keys are disjoint (regular = adHoc false, ad-hoc = adHoc true);
// ALL/Active/Passive carry no adHoc constraint.
describe("filterKeyToQuery", () => {
  it("ALL applies no constraint (neither mode nor adHoc)", () => {
    const q = filterKeyToQuery("ALL");
    expect(q.mode).toBeUndefined();
    expect("adHoc" in q ? q.adHoc : undefined).toBeUndefined();
  });

  it("DATAHUB_MANAGED_REGULAR → mode DATAHUB_MANAGED + adHoc false", () => {
    expect(filterKeyToQuery("DATAHUB_MANAGED_REGULAR")).toEqual({
      mode: "DATAHUB_MANAGED",
      adHoc: false,
    });
  });

  it("DATAHUB_MANAGED_AD_HOC → mode DATAHUB_MANAGED + adHoc true", () => {
    expect(filterKeyToQuery("DATAHUB_MANAGED_AD_HOC")).toEqual({
      mode: "DATAHUB_MANAGED",
      adHoc: true,
    });
  });

  it("ACTIVE_CUSTOM_MANAGED → mode only, adHoc absent", () => {
    const q = filterKeyToQuery("ACTIVE_CUSTOM_MANAGED");
    expect(q.mode).toBe("ACTIVE_CUSTOM_MANAGED");
    expect("adHoc" in q).toBe(false);
  });

  it("PASSIVE → mode only, adHoc absent", () => {
    const q = filterKeyToQuery("PASSIVE");
    expect(q.mode).toBe("PASSIVE");
    expect("adHoc" in q).toBe(false);
  });
});

describe("filterKeyLabel", () => {
  it("returns a distinct non-empty label for every filter key", () => {
    const labels = INGESTION_FILTER_KEYS.map(filterKeyLabel);
    for (const label of labels) {
      expect(label.length).toBeGreaterThan(0);
    }
    // Labels are distinct so the dropdown options are unambiguous.
    expect(new Set(labels).size).toBe(labels.length);
  });
});

describe("INGESTION_FILTER_KEYS", () => {
  it("lists exactly the 5 filter keys in display order", () => {
    expect(INGESTION_FILTER_KEYS).toEqual([
      "ALL",
      "DATAHUB_MANAGED_REGULAR",
      "DATAHUB_MANAGED_AD_HOC",
      "ACTIVE_CUSTOM_MANAGED",
      "PASSIVE",
    ] satisfies IngestionFilterKey[]);
  });
});

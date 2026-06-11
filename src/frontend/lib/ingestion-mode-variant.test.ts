import { describe, it, expect } from "vitest";
import {
  modeBadgeVariant,
  modeLabel,
  modeDescription,
  scheduleTierLabel,
  TIER_TO_CANONICAL_CRON,
} from "./ingestion-mode-variant";

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

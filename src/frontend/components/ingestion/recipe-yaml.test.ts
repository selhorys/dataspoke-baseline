import { describe, it, expect } from "vitest";
import {
  sourceBodyToYaml,
  parseSourceYaml,
  validateSourceBody,
  findSecretRefs,
  toEditableBody,
} from "./recipe-yaml";
import type { IngestionSource, IngestionSourceBody } from "@/types/ingestion";

const fullSource: IngestionSource = {
  id: "src-1",
  mode: "ACTIVE_CUSTOM_MANAGED",
  name: "prod postgres catalog",
  schedule: "0 0 * * *",
  recipe: {
    source: {
      type: "postgres",
      config: {
        host_port: "pg.example:5432",
        username: "spoke_reader",
        password: "${dummy-data-pg__password}",
        schema_pattern: { allow: ["^catalog$"] },
        env: "DEV",
      },
    },
  },
  platform: "postgres",
  status: "OK",
  datahub_source_urn: null,
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-02T00:00:00Z",
};

describe("toEditableBody / sourceBodyToYaml", () => {
  it("strips read-only fields", () => {
    const yaml = sourceBodyToYaml(fullSource);
    expect(yaml).not.toContain("src-1");
    expect(yaml).not.toContain("platform");
    expect(yaml).not.toContain("status");
    expect(yaml).not.toContain("created_at");
    expect(yaml).toContain("mode:");
    expect(yaml).toContain("name:");
    expect(yaml).toContain("schedule:");
    expect(yaml).toContain("recipe:");
  });

  it("toEditableBody keeps only the four editable fields", () => {
    const body = toEditableBody(fullSource);
    expect(Object.keys(body).sort()).toEqual([
      "mode",
      "name",
      "recipe",
      "schedule",
    ]);
  });
});

describe("round-trip YAML ⇄ JSON", () => {
  it("round-trips deep-equal including secret refs", () => {
    const body = toEditableBody(fullSource);
    const yaml = sourceBodyToYaml(body);
    const parsed = parseSourceYaml(yaml);
    expect(parsed.ok).toBe(true);
    expect(parsed.value).toEqual(body);
  });

  it("preserves a null schedule explicitly", () => {
    const body: IngestionSourceBody = {
      mode: "PASSIVE",
      name: "passive lake scope",
      schedule: null,
      recipe: { source: { type: "s3", config: {} } },
    };
    const yaml = sourceBodyToYaml(body);
    const parsed = parseSourceYaml(yaml);
    expect(parsed.ok).toBe(true);
    expect((parsed.value as IngestionSourceBody).schedule).toBeNull();
  });

  it("preserves secret refs verbatim through the round trip", () => {
    const yaml = sourceBodyToYaml(fullSource);
    const parsed = parseSourceYaml(yaml);
    const cfg = (
      (parsed.value as IngestionSourceBody).recipe as {
        source: { config: Record<string, unknown> };
      }
    ).source.config;
    expect(cfg.password).toBe("${dummy-data-pg__password}");
  });
});

describe("parseSourceYaml errors", () => {
  it("reports an empty body", () => {
    const r = parseSourceYaml("   ");
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/empty/i);
  });

  it("reports a parse error with a line number", () => {
    const r = parseSourceYaml("mode: ACTIVE\nname: [unterminated");
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/line \d+/);
  });
});

describe("validateSourceBody", () => {
  const validActive = {
    mode: "ACTIVE_CUSTOM_MANAGED",
    name: "x",
    schedule: "0 0 * * *",
    recipe: { source: { type: "postgres", config: {} } },
  };

  it("accepts a valid ACTIVE body", () => {
    const r = validateSourceBody(validActive, { creatableOnly: true });
    expect(r.ok).toBe(true);
    expect(r.body?.mode).toBe("ACTIVE_CUSTOM_MANAGED");
  });

  it("rejects a non-mapping", () => {
    expect(validateSourceBody("nope").ok).toBe(false);
    expect(validateSourceBody([]).ok).toBe(false);
    expect(validateSourceBody(null).ok).toBe(false);
  });

  it("rejects an unknown mode", () => {
    const r = validateSourceBody({ ...validActive, mode: "BOGUS" });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/mode/);
  });

  it("rejects DATAHUB_MANAGED when creatableOnly", () => {
    const r = validateSourceBody(
      { ...validActive, mode: "DATAHUB_MANAGED" },
      { creatableOnly: true },
    );
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/not creatable/i);
  });

  it("rejects a name that is empty or too long", () => {
    expect(validateSourceBody({ ...validActive, name: "" }).ok).toBe(false);
    expect(
      validateSourceBody({ ...validActive, name: "a".repeat(513) }).ok,
    ).toBe(false);
  });

  it("rejects a non-canonical cron for ACTIVE", () => {
    const r = validateSourceBody({ ...validActive, schedule: "15 3 * * 1" });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/canonical/i);
  });

  it("accepts a null schedule for ACTIVE", () => {
    const r = validateSourceBody({ ...validActive, schedule: null });
    expect(r.ok).toBe(true);
  });

  it("rejects a schedule on a PASSIVE source", () => {
    const r = validateSourceBody({
      mode: "PASSIVE",
      name: "x",
      schedule: "0 0 * * *",
      recipe: { source: { type: "s3", config: {} } },
    });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/PASSIVE/);
  });

  it("rejects a recipe missing source.type", () => {
    const r = validateSourceBody({
      ...validActive,
      recipe: { source: { config: {} } },
    });
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/source\.type/);
  });

  describe("recipeOnly", () => {
    it("validates a bare recipe object and skips name/schedule/mode", () => {
      const r = validateSourceBody(
        { source: { type: "postgres", config: { host: "x" } } },
        { recipeOnly: true },
      );
      expect(r.ok).toBe(true);
      expect(r.recipe).toMatchObject({ source: { type: "postgres" } });
      // recipeOnly does not produce a full body — the page composes that.
      expect(r.body).toBeUndefined();
    });

    it("rejects a bare recipe missing source.type", () => {
      const r = validateSourceBody(
        { source: { config: {} } },
        { recipeOnly: true },
      );
      expect(r.ok).toBe(false);
      expect(r.error).toMatch(/source\.type/);
    });
  });
});

describe("findSecretRefs", () => {
  it("finds distinct secret refs", () => {
    const text =
      'password: ${a__p}\ntoken: ${b__t}\nother: ${a__p}\nplain: nope';
    expect(findSecretRefs(text).sort()).toEqual(["${a__p}", "${b__t}"]);
  });

  it("returns empty when none present", () => {
    expect(findSecretRefs("password: plain")).toEqual([]);
  });
});

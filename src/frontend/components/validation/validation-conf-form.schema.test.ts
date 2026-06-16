/**
 * Tests for validation-conf-form.schema.ts — Zod schema invariants and
 * serialization helpers (toInternal / fromInternal / defaultFormValues).
 *
 * Spec traces:
 *   - spec/feature/VALIDATION.md §Rule Configuration:
 *     description: ≤ 2,000 chars; no ASCII control chars except \t (0x09) and \n (0x0a)
 *     variables: each name matches \A[a-z][a-z0-9_]{0,99}\Z; unique; 1–200 entries
 *   - src/api/schemas/validation.py ValidationVariable, _VARIABLE_RE, _DESC_CTRL_RE:
 *     _VARIABLE_RE = r"\A[a-z][a-z0-9_]{0,99}\Z"
 *     _DESC_CTRL_RE = r"[\x00-\x08\x0b-\x1f\x7f]" (excludes \t=0x09, \n=0x0a)
 *     min 1 / max 200 variable entries; each variable = { name, description }
 *     variable description: required key, ≤ 200 chars, empty allowed, same control-char rule
 *   - spec/feature/FRONTEND_VALIDATION.md §Page contracts:
 *     "field constraints (rule-description char cap, variable name regex,
 *      per-variable description ≤200 chars empty-allowed, count cap)"
 */

import { describe, it, expect } from "vitest";
import {
  validationConfSchema,
  toInternal,
  fromInternal,
  defaultFormValues,
  VARIABLE_NAME_RE,
  variableNameError,
} from "./validation-conf-form.schema";
import type { ValidationConfFormValues } from "@/types/validation";
import type { ValidationConfResponse } from "@/types/validation";

// ── Helpers ────────────────────────────────────────────────────────────────────

function makeValidForm(overrides: Partial<ValidationConfFormValues> = {}): ValidationConfFormValues {
  return {
    description: "Daily row count plus key column means and null counts",
    variables: [{ name: "row_cnt", description: "Daily row count" }],
    ...overrides,
  };
}

function makeVariables(count: number): { name: string; description: string }[] {
  return Array.from({ length: count }, (_, i) => ({
    name: `var_${i}`,
    description: "",
  }));
}

// ── 1. Variable name regex — valid names ──────────────────────────────────────
//
// Backend: _VARIABLE_RE = r"\A[a-z][a-z0-9_]{0,99}\Z"
// Total length: 1 (leading letter) + up to 99 trailing = 1–100 chars.

describe("VARIABLE_NAME_RE — valid names (src/api/schemas/validation.py _VARIABLE_RE)", () => {
  it("accepts single lowercase letter 'x'", () => {
    expect(VARIABLE_NAME_RE.test("x")).toBe(true);
  });

  it("accepts 'col_1' (lowercase, digit, underscore)", () => {
    expect(VARIABLE_NAME_RE.test("col_1")).toBe(true);
  });

  it("accepts 'a_b_c' (underscores between letters)", () => {
    expect(VARIABLE_NAME_RE.test("a_b_c")).toBe(true);
  });

  it("accepts 'row_cnt' (typical variable name)", () => {
    expect(VARIABLE_NAME_RE.test("row_cnt")).toBe(true);
  });

  it("accepts 'qty_negative_cnt' (multi-segment with underscores)", () => {
    expect(VARIABLE_NAME_RE.test("qty_negative_cnt")).toBe(true);
  });

  it("accepts exactly 100-char name (1 leading letter + 99 trailing chars) — boundary OK", () => {
    // Backend: {0,99} means 0 to 99 additional chars after the first → max total = 100
    const name = "a" + "b".repeat(99);
    expect(name.length).toBe(100);
    expect(VARIABLE_NAME_RE.test(name)).toBe(true);
  });

  it("accepts name with digits after the leading letter: 'col1'", () => {
    expect(VARIABLE_NAME_RE.test("col1")).toBe(true);
  });

  it("accepts 'a0_b1_c2' (interleaved digits)", () => {
    expect(VARIABLE_NAME_RE.test("a0_b1_c2")).toBe(true);
  });
});

// ── 2. Variable name regex — invalid names ────────────────────────────────────

describe("VARIABLE_NAME_RE — invalid names (src/api/schemas/validation.py _VARIABLE_RE)", () => {
  it("rejects uppercase letter in name: 'ColName'", () => {
    expect(VARIABLE_NAME_RE.test("ColName")).toBe(false);
  });

  it("rejects leading digit: '1col'", () => {
    expect(VARIABLE_NAME_RE.test("1col")).toBe(false);
  });

  it("rejects leading underscore: '_col'", () => {
    expect(VARIABLE_NAME_RE.test("_col")).toBe(false);
  });

  it("rejects hyphen: 'col-name'", () => {
    expect(VARIABLE_NAME_RE.test("col-name")).toBe(false);
  });

  it("rejects space: 'col name'", () => {
    expect(VARIABLE_NAME_RE.test("col name")).toBe(false);
  });

  it("rejects empty string", () => {
    expect(VARIABLE_NAME_RE.test("")).toBe(false);
  });

  it("rejects 101-char name — one over the 100-char boundary", () => {
    // Backend {0,99} allows up to 99 trailing → max total 100; 101 must fail.
    const name = "a" + "b".repeat(100);
    expect(name.length).toBe(101);
    expect(VARIABLE_NAME_RE.test(name)).toBe(false);
  });

  it("rejects ALL-UPPERCASE name: 'ROWCOUNT'", () => {
    expect(VARIABLE_NAME_RE.test("ROWCOUNT")).toBe(false);
  });

  it("rejects name containing dot: 'col.name'", () => {
    expect(VARIABLE_NAME_RE.test("col.name")).toBe(false);
  });
});

// ── 3. variableNameError helper ───────────────────────────────────────────────

describe("variableNameError — returns null for valid, string for invalid (mirrors _VARIABLE_RE)", () => {
  it("returns null for 'row_cnt'", () => {
    expect(variableNameError("row_cnt")).toBeNull();
  });

  it("returns null for single char 'x'", () => {
    expect(variableNameError("x")).toBeNull();
  });

  it("returns an error string for uppercase name", () => {
    expect(variableNameError("ColName")).not.toBeNull();
    expect(typeof variableNameError("ColName")).toBe("string");
  });

  it("returns an error string for leading underscore", () => {
    expect(variableNameError("_col")).not.toBeNull();
  });

  it("returns an error string for empty string", () => {
    expect(variableNameError("")).not.toBeNull();
  });
});

// ── 4. Schema: description field constraints ──────────────────────────────────
//
// Backend: _DESC_CTRL_RE = r"[\x00-\x08\x0b-\x1f\x7f]"
// Permitted: ordinary text, \t (0x09), \n (0x0a)
// Rejected: any char in [\x00-\x08], [\x0b-\x1f], \x7f

describe("schema description — length and control-char rules (VALIDATION.md §Rule Configuration)", () => {
  it("accepts empty string description (empty is allowed — only control chars and length are constrained)", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "" }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts a normal ASCII description", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "Daily row count check" }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts exactly 2000 chars — at the boundary", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "a".repeat(2000) }),
    );
    expect(result.success).toBe(true);
  });

  it("rejects 2001 chars — one over the 2000-char cap", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "a".repeat(2001) }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.some((i) => i.path[0] === "description")).toBe(true);
    }
  });

  it("accepts tab character \\t (0x09) — excluded from the rejection set", () => {
    // Backend _DESC_CTRL_RE excludes 0x09 (\t) from the rejection range [\x00-\x08\x0b-\x1f]
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "row\tcount" }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts newline character \\n (0x0a) — excluded from the rejection set", () => {
    // Backend _DESC_CTRL_RE excludes 0x0a (\n) — range is \x00-\x08 then \x0b-\x1f
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "row\ncount" }),
    );
    expect(result.success).toBe(true);
  });

  it("rejects NUL character \\x00 — in rejection set [\x00-\x08]", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "row\x00count" }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.some((i) => i.path[0] === "description")).toBe(true);
    }
  });

  it("rejects \\x01 — in rejection set [\x00-\x08]", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "row\x01count" }),
    );
    expect(result.success).toBe(false);
  });

  it("rejects \\x08 (backspace) — last char in first rejection range [\x00-\x08]", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "row\x08count" }),
    );
    expect(result.success).toBe(false);
  });

  it("rejects \\x0b (vertical tab) — first char in second rejection range [\x0b-\x1f]", () => {
    // 0x0a is allowed; 0x0b starts the second exclusion range
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "row\x0bcount" }),
    );
    expect(result.success).toBe(false);
  });

  it("rejects \\x0c (form feed) — in rejection set [\x0b-\x1f]", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "row\x0ccount" }),
    );
    expect(result.success).toBe(false);
  });

  it("rejects \\x1f — last char in second rejection range [\x0b-\x1f]", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "row\x1fcount" }),
    );
    expect(result.success).toBe(false);
  });

  it("rejects DEL character \\x7f — explicitly in rejection set", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ description: "row\x7fcount" }),
    );
    expect(result.success).toBe(false);
  });
});

// ── 5. Schema: variable count bounds ─────────────────────────────────────────
//
// Backend: min 1, max 200 entries; error attached at array root.

describe("schema variables — count bounds 1–200 (VALIDATION.md §Rule Configuration)", () => {
  it("rejects 0 variables — must have at least 1 entry", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ variables: [] }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      // The min-1 error must be on the "variables" path (array root).
      expect(result.error.issues.some((i) => i.path[0] === "variables")).toBe(true);
    }
  });

  it("accepts exactly 1 variable — lower bound OK", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ variables: [{ name: "row_cnt", description: "" }] }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts exactly 200 variables — upper bound OK", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ variables: makeVariables(200) }),
    );
    expect(result.success).toBe(true);
  });

  it("rejects 201 variables — one over the 200-entry cap", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ variables: makeVariables(201) }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.some((i) => i.path[0] === "variables")).toBe(true);
    }
  });
});

// ── 6. Schema: variable name uniqueness via superRefine ──────────────────────
//
// Backend: _validate_variables raises if len(variables) != len(set(variables))
// Frontend: superRefine adds an issue on ["variables", idx, "name"] for the duplicate.

describe("schema variables — uniqueness enforced by superRefine (VALIDATION.md §Rule Configuration)", () => {
  it("rejects duplicate variable names with an issue on the duplicate row's path", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [
          { name: "row_cnt", description: "" },
          { name: "col1_mean", description: "" },
          { name: "row_cnt", description: "" },
        ],
      }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      // The duplicate is at index 2 — issue path must be ["variables", 2, "name"]
      const dupIssue = result.error.issues.find(
        (i) =>
          Array.isArray(i.path) &&
          i.path[0] === "variables" &&
          i.path[1] === 2 &&
          i.path[2] === "name",
      );
      expect(dupIssue).toBeDefined();
    }
  });

  it("rejects two identical entries at index 1 — issue on [variables, 1, name]", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [
          { name: "row_cnt", description: "" },
          { name: "row_cnt", description: "" },
        ],
      }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      const dupIssue = result.error.issues.find(
        (i) =>
          Array.isArray(i.path) &&
          i.path[0] === "variables" &&
          i.path[1] === 1 &&
          i.path[2] === "name",
      );
      expect(dupIssue).toBeDefined();
    }
  });

  it("accepts two distinct variable names without duplicate issue", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [
          { name: "row_cnt", description: "" },
          { name: "col1_mean", description: "" },
        ],
      }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts 5 distinct variable names without duplicate issue", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [
          { name: "row_cnt", description: "" },
          { name: "col1_mean", description: "" },
          { name: "col2_null_cnt", description: "" },
          { name: "user_id_null_cnt", description: "" },
          { name: "qty_total", description: "" },
        ],
      }),
    );
    expect(result.success).toBe(true);
  });
});

// ── 7. Schema: variable name regex validated by schema ────────────────────────

describe("schema variables — name regex enforcement (src/api/schemas/validation.py _VARIABLE_RE)", () => {
  it("rejects uppercase variable name 'RowCnt'", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ variables: [{ name: "RowCnt", description: "" }] }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      const issue = result.error.issues.find(
        (i) => Array.isArray(i.path) && i.path[0] === "variables" && i.path[1] === 0,
      );
      expect(issue).toBeDefined();
    }
  });

  it("rejects leading-digit variable name '1count'", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ variables: [{ name: "1count", description: "" }] }),
    );
    expect(result.success).toBe(false);
  });

  it("rejects 101-char variable name", () => {
    const longName = "a" + "b".repeat(100); // 101 chars
    const result = validationConfSchema.safeParse(
      makeValidForm({ variables: [{ name: longName, description: "" }] }),
    );
    expect(result.success).toBe(false);
  });

  it("accepts exactly 100-char variable name — at the backend boundary", () => {
    const maxName = "a" + "b".repeat(99); // 100 chars
    const result = validationConfSchema.safeParse(
      makeValidForm({ variables: [{ name: maxName, description: "" }] }),
    );
    expect(result.success).toBe(true);
  });
});

// ── 8. Schema: per-variable description constraints ───────────────────────────
//
// Backend: ValidationVariable.description — required key, ≤ 200 chars, empty allowed,
// same control-char rule as the rule description (_DESC_CTRL_RE).

describe("schema variable description — ≤200 chars, empty allowed, control-char rule", () => {
  it("accepts an empty variable description (empty is allowed)", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({ variables: [{ name: "row_cnt", description: "" }] }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts a normal variable description", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [{ name: "row_cnt", description: "Daily row count" }],
      }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts exactly 200 chars — at the boundary", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [{ name: "row_cnt", description: "a".repeat(200) }],
      }),
    );
    expect(result.success).toBe(true);
  });

  it("rejects 201 chars — one over the 200-char cap", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [{ name: "row_cnt", description: "a".repeat(201) }],
      }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      const issue = result.error.issues.find(
        (i) =>
          Array.isArray(i.path) &&
          i.path[0] === "variables" &&
          i.path[1] === 0 &&
          i.path[2] === "description",
      );
      expect(issue).toBeDefined();
    }
  });

  it("accepts tab \\t (0x09) in a variable description — excluded from rejection set", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [{ name: "row_cnt", description: "row\tcount" }],
      }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts newline \\n (0x0a) in a variable description — excluded from rejection set", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [{ name: "row_cnt", description: "row\ncount" }],
      }),
    );
    expect(result.success).toBe(true);
  });

  it("rejects NUL \\x00 in a variable description — in rejection set", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [{ name: "row_cnt", description: "row\x00count" }],
      }),
    );
    expect(result.success).toBe(false);
  });

  it("rejects DEL \\x7f in a variable description — explicitly in rejection set", () => {
    const result = validationConfSchema.safeParse(
      makeValidForm({
        variables: [{ name: "row_cnt", description: "row\x7fcount" }],
      }),
    );
    expect(result.success).toBe(false);
  });
});

// ── 9. toInternal: API response → form shape ──────────────────────────────────
//
// Spec: toInternal converts a ValidationConfResponse into ValidationConfFormValues.
// variables in the response are { name, description } objects.

describe("toInternal — converts ValidationConfResponse into form shape (FRONTEND_VALIDATION.md §Page contracts)", () => {
  const apiResponse: ValidationConfResponse = {
    dataset_urn: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
    description: "Daily row count check",
    variables: [
      { name: "row_cnt", description: "Daily row count" },
      { name: "col1_mean", description: "Mean of col1" },
      { name: "col2_null_cnt", description: "" },
    ],
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-08T10:00:00Z",
  };

  it("maps description directly from the response", () => {
    const form = toInternal(apiResponse);
    expect(form.description).toBe("Daily row count check");
  });

  it("maps each variable's name and description into the form shape", () => {
    const form = toInternal(apiResponse);
    expect(form.variables).toEqual([
      { name: "row_cnt", description: "Daily row count" },
      { name: "col1_mean", description: "Mean of col1" },
      { name: "col2_null_cnt", description: "" },
    ]);
  });

  it("preserves variable count (3 → 3 items)", () => {
    const form = toInternal(apiResponse);
    expect(form.variables).toHaveLength(3);
  });

  it("maps an empty-string rule description (server returned empty)", () => {
    const form = toInternal({ ...apiResponse, description: "" });
    expect(form.description).toBe("");
  });

  it("maps a single-variable response", () => {
    const form = toInternal({
      ...apiResponse,
      variables: [{ name: "row_cnt", description: "Daily row count" }],
    });
    expect(form.variables).toEqual([
      { name: "row_cnt", description: "Daily row count" },
    ]);
  });
});

// ── 10. fromInternal: form shape → API request body ───────────────────────────
//
// Spec: PUT .../attr/validation/conf body = { description, variables: {name, description}[] }
// Backend: PutValidationConfRequest

describe("fromInternal — serializes form values to API request body (VALIDATION.md §Rule Configuration)", () => {
  it("produces a 'description' string field", () => {
    const body = fromInternal(makeValidForm());
    expect(typeof body.description).toBe("string");
    expect(body.description).toBe("Daily row count plus key column means and null counts");
  });

  it("produces a 'variables' array of { name, description } objects", () => {
    const body = fromInternal(
      makeValidForm({
        variables: [
          { name: "row_cnt", description: "Daily row count" },
          { name: "col1_mean", description: "Mean of col1" },
        ],
      }),
    );
    expect(body.variables).toEqual([
      { name: "row_cnt", description: "Daily row count" },
      { name: "col1_mean", description: "Mean of col1" },
    ]);
  });

  it("preserves empty per-variable descriptions verbatim", () => {
    const body = fromInternal(
      makeValidForm({
        variables: [{ name: "row_cnt", description: "" }],
      }),
    );
    expect(body.variables).toEqual([{ name: "row_cnt", description: "" }]);
  });

  it("serializes empty-string rule description verbatim (backend allows empty)", () => {
    const body = fromInternal(makeValidForm({ description: "" }));
    expect(body.description).toBe("");
  });

  it("produces exactly the two keys 'description' and 'variables' in the body", () => {
    const body = fromInternal(makeValidForm());
    expect(Object.keys(body).sort()).toEqual(["description", "variables"]);
  });
});

// ── 11. Round-trip: toInternal(response) → fromInternal ────────────────────────
//
// Spec: editing and saving should produce a body that round-trips through toInternal
// without losing field values.

describe("round-trip toInternal(response) → fromInternal — preserves variables and description", () => {
  const apiResponse: ValidationConfResponse = {
    dataset_urn: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)",
    description: "Row count and anomaly markers for daily fulfillment",
    variables: [
      { name: "row_cnt", description: "Daily row count" },
      { name: "anomaly_flag", description: "" },
      { name: "qty_total", description: "Total quantity" },
    ],
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-08T12:00:00Z",
  };

  it("round-trip preserves description", () => {
    const body = fromInternal(toInternal(apiResponse));
    expect(body.description).toBe(
      "Row count and anomaly markers for daily fulfillment",
    );
  });

  it("round-trip preserves all variable names and descriptions", () => {
    const body = fromInternal(toInternal(apiResponse));
    expect(body.variables).toEqual([
      { name: "row_cnt", description: "Daily row count" },
      { name: "anomaly_flag", description: "" },
      { name: "qty_total", description: "Total quantity" },
    ]);
  });

  it("round-trip body passes the validationConfSchema (valid API body)", () => {
    const formValues = toInternal(apiResponse);
    const schemaResult = validationConfSchema.safeParse(formValues);
    expect(schemaResult.success).toBe(true);
  });

  it("round-trip with empty-string rule description (empty is valid)", () => {
    const response: ValidationConfResponse = {
      ...apiResponse,
      description: "",
    };
    const body = fromInternal(toInternal(response));
    expect(body.description).toBe("");
  });
});

// ── 12. defaultFormValues ─────────────────────────────────────────────────────

describe("defaultFormValues — blank form state for a new config", () => {
  it("returns an empty-string description", () => {
    expect(defaultFormValues().description).toBe("");
  });

  it("returns a single empty variable row with empty name and description", () => {
    expect(defaultFormValues().variables).toEqual([
      { name: "", description: "" },
    ]);
  });

  it("has exactly 2 top-level keys: description and variables", () => {
    const v = defaultFormValues();
    expect(Object.keys(v).sort()).toEqual(["description", "variables"]);
  });
});

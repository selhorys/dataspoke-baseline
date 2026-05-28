/**
 * Tests for forgot-password form schema.
 *
 * Spec traces:
 *   - spec/API.md §POST /auth/password/reset/request: email required and valid
 *   - spec/feature/FRONTEND_BASIC.md §Routing: /forgot-password submits POST /auth/password/reset/request
 */
import { describe, it, expect } from "vitest";
import { forgotPasswordSchema } from "./forgot-password.schema";

describe("forgotPasswordSchema — email validation", () => {
  it("rejects a malformed email address", () => {
    const result = forgotPasswordSchema.safeParse({ email: "not-an-email" });
    expect(result.success).toBe(false);
    if (!result.success) {
      const err = result.error.issues.find((i) => i.path[0] === "email");
      expect(err).toBeDefined();
    }
  });

  it("rejects an email missing @", () => {
    const result = forgotPasswordSchema.safeParse({ email: "userexample.com" });
    expect(result.success).toBe(false);
  });

  it("accepts a valid email address", () => {
    const result = forgotPasswordSchema.safeParse({ email: "user@imazon.example.com" });
    expect(result.success).toBe(true);
  });

  it("fails when email is empty string", () => {
    const result = forgotPasswordSchema.safeParse({ email: "" });
    expect(result.success).toBe(false);
  });

  it("fails when email field is missing", () => {
    const result = forgotPasswordSchema.safeParse({});
    expect(result.success).toBe(false);
  });
});

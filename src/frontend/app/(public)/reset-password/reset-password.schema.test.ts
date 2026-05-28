/**
 * Tests for reset-password form schema.
 *
 * Spec traces:
 *   - spec/API.md §POST /auth/password/reset/confirm: new_password minimum 10 chars
 *   - spec/feature/FRONTEND_BASIC.md §Routing: /reset-password submits POST /auth/password/reset/confirm
 */
import { describe, it, expect } from "vitest";
import { resetPasswordSchema } from "./reset-password.schema";

describe("resetPasswordSchema — new_password validation", () => {
  it("rejects a password shorter than 10 characters", () => {
    const result = resetPasswordSchema.safeParse({ new_password: "short123" });
    expect(result.success).toBe(false);
    if (!result.success) {
      const err = result.error.issues.find((i) => i.path[0] === "new_password");
      expect(err).toBeDefined();
    }
  });

  it("accepts a password of exactly 10 characters", () => {
    const result = resetPasswordSchema.safeParse({ new_password: "a".repeat(10) });
    expect(result.success).toBe(true);
  });

  it("accepts a password longer than 10 characters", () => {
    const result = resetPasswordSchema.safeParse({ new_password: "securepassword99!" });
    expect(result.success).toBe(true);
  });

  it("rejects a password longer than 128 characters", () => {
    const result = resetPasswordSchema.safeParse({ new_password: "x".repeat(129) });
    expect(result.success).toBe(false);
    if (!result.success) {
      const err = result.error.issues.find((i) => i.path[0] === "new_password");
      expect(err).toBeDefined();
    }
  });

  it("rejects an empty password", () => {
    const result = resetPasswordSchema.safeParse({ new_password: "" });
    expect(result.success).toBe(false);
  });

  it("fails when new_password is missing entirely", () => {
    const result = resetPasswordSchema.safeParse({});
    expect(result.success).toBe(false);
  });
});

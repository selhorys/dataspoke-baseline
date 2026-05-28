/**
 * Tests for register form schema.
 *
 * Spec traces:
 *   - spec/API.md §POST /auth/register: email + name + password fields;
 *     password minimum 10 chars enforced at the UI layer before submission.
 *   - spec/feature/FRONTEND_BASIC.md §Register form validation
 */
import { describe, it, expect } from "vitest";
import { registerSchema } from "./register.schema";

describe("registerSchema — password validation", () => {
  it("rejects a password shorter than 10 characters", () => {
    const result = registerSchema.safeParse({
      email: "user@example.com",
      name: "Alice",
      password: "short123",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const pwErrors = result.error.issues.filter((i) => i.path[0] === "password");
      expect(pwErrors.length).toBeGreaterThan(0);
    }
  });

  it("accepts a password of exactly 10 characters", () => {
    const result = registerSchema.safeParse({
      email: "user@example.com",
      name: "Alice",
      password: "a".repeat(10),
    });
    expect(result.success).toBe(true);
  });

  it("accepts a password longer than 10 characters", () => {
    const result = registerSchema.safeParse({
      email: "user@example.com",
      name: "Alice",
      password: "strongpassword123!",
    });
    expect(result.success).toBe(true);
  });

  it("rejects a password longer than 128 characters", () => {
    const result = registerSchema.safeParse({
      email: "user@example.com",
      name: "Alice",
      password: "a".repeat(129),
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const pwErrors = result.error.issues.filter((i) => i.path[0] === "password");
      expect(pwErrors.length).toBeGreaterThan(0);
    }
  });
});

describe("registerSchema — email validation", () => {
  it("rejects a malformed email address", () => {
    const result = registerSchema.safeParse({
      email: "not-an-email",
      name: "Alice",
      password: "validpassword!",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const emailErrors = result.error.issues.filter((i) => i.path[0] === "email");
      expect(emailErrors.length).toBeGreaterThan(0);
    }
  });

  it("rejects an email missing the @ symbol", () => {
    const result = registerSchema.safeParse({
      email: "userexample.com",
      name: "Alice",
      password: "validpassword!",
    });
    expect(result.success).toBe(false);
  });

  it("accepts a valid email address", () => {
    const result = registerSchema.safeParse({
      email: "alice@imazon.example.com",
      name: "Alice",
      password: "validpassword!",
    });
    expect(result.success).toBe(true);
  });
});

describe("registerSchema — name validation", () => {
  it("rejects an empty name", () => {
    const result = registerSchema.safeParse({
      email: "user@example.com",
      name: "",
      password: "validpassword!",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const nameErrors = result.error.issues.filter((i) => i.path[0] === "name");
      expect(nameErrors.length).toBeGreaterThan(0);
    }
  });

  it("rejects a name longer than 128 characters", () => {
    const result = registerSchema.safeParse({
      email: "user@example.com",
      name: "A".repeat(129),
      password: "validpassword!",
    });
    expect(result.success).toBe(false);
  });

  it("accepts a name within allowed length", () => {
    const result = registerSchema.safeParse({
      email: "user@example.com",
      name: "Alice Wonderland",
      password: "validpassword!",
    });
    expect(result.success).toBe(true);
  });
});

describe("registerSchema — required fields", () => {
  it("fails when email is missing", () => {
    const result = registerSchema.safeParse({ name: "Alice", password: "validpassword!" });
    expect(result.success).toBe(false);
  });

  it("fails when name is missing", () => {
    const result = registerSchema.safeParse({ email: "user@example.com", password: "validpassword!" });
    expect(result.success).toBe(false);
  });

  it("fails when password is missing", () => {
    const result = registerSchema.safeParse({ email: "user@example.com", name: "Alice" });
    expect(result.success).toBe(false);
  });
});

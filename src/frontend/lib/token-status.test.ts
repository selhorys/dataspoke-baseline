/**
 * Tests for lib/token-status.ts — the three-state API-token classifier that the
 * Status column of both admin token views is derived from.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §API tokens (`/profile/tokens`): "Status is
 *     one of **active**, **expired**, or **revoked**, derived client-side:
 *     `revoked` when `revoked_at` is set, else `expired` when `expires_at` has
 *     passed, else `active`. Revocation wins over expiry, being the deliberate
 *     act."
 *   - same section: "The expired state exists because the route filters on
 *     `revoked_at` alone, so an expired token sits in the default page while
 *     authenticating nothing — labelling it 'active' on the one view built to
 *     answer what can reach the deployment would over-count exactly what an
 *     operator came to count."
 *   - spec/feature/AUTH.md §Revoked-token visibility: "`revoked_at IS NULL` is
 *     the whole of the default filter. Expiry is not filtered".
 *   - spec/feature/FRONTEND_BASIC.md §API tokens ASCII: a row whose Expires
 *     column reads "never" (no `expires_at`) is listed `active`; the revoked row
 *     in the All-tokens block also carries "never" and still reads `revoked`.
 *
 * `now` is injected on every call rather than mocked on the global clock: the
 * function takes it as a parameter precisely so a classification can be pinned
 * to a fixed instant, and a frozen global clock would leave the parameter
 * itself untested.
 */

import { describe, it, expect } from "vitest";
import { tokenStatus } from "./token-status";

/** The instant every case below is classified against. */
const NOW = new Date("2026-06-01T12:00:00.000Z");

const PAST = "2026-05-31T12:00:00.000Z";
const FUTURE = "2026-07-01T12:00:00.000Z";

// ── Revocation wins ───────────────────────────────────────────────────────────

describe("tokenStatus — revocation outranks every other signal", () => {
  it("reports 'revoked' for a token that is both revoked and past its expiry", () => {
    // Both signals are true at once — the only input that can tell an
    // expiry-first implementation from a revocation-first one.
    expect(
      tokenStatus({ revoked_at: "2026-04-01T00:00:00.000Z", expires_at: PAST }, NOW),
    ).toBe("revoked");
  });

  it("reports 'revoked' for a revoked token whose expiry has not arrived", () => {
    expect(
      tokenStatus({ revoked_at: "2026-04-01T00:00:00.000Z", expires_at: FUTURE }, NOW),
    ).toBe("revoked");
  });

  it("reports 'revoked' for a revoked token that never expires", () => {
    // The All-tokens ASCII block's bob@imazon row: Expires "never", Status
    // "revoked" — the absent stamp must not short-circuit past revocation.
    expect(tokenStatus({ revoked_at: "2026-04-01T00:00:00.000Z", expires_at: null }, NOW)).toBe(
      "revoked",
    );
  });

  it("treats a null revoked_at as not revoked, so the field is read for content and not presence", () => {
    expect(tokenStatus({ revoked_at: null, expires_at: FUTURE }, NOW)).toBe("active");
  });
});

// ── Expiry ────────────────────────────────────────────────────────────────────

describe("tokenStatus — expiry of an unrevoked token", () => {
  it("reports 'expired' for an unrevoked token whose expires_at has passed", () => {
    expect(tokenStatus({ revoked_at: null, expires_at: PAST }, NOW)).toBe("expired");
  });

  it("reports 'active' for an unrevoked token whose expires_at is still ahead", () => {
    expect(tokenStatus({ revoked_at: null, expires_at: FUTURE }, NOW)).toBe("active");
  });

  it("reports 'expired' at exactly now — the instant the credential stops reaching the deployment", () => {
    // The tie-break sits on the side that does not over-count: at its own
    // expiry stamp the token authenticates nothing, and the inventory exists to
    // answer what can reach the deployment.
    expect(tokenStatus({ revoked_at: null, expires_at: NOW.toISOString() }, NOW)).toBe("expired");
  });

  it("reports 'active' one millisecond before the stamp, so the boundary is a boundary and not a floor", () => {
    const oneMsAfterNow = new Date(NOW.getTime() + 1).toISOString();
    expect(tokenStatus({ revoked_at: null, expires_at: oneMsAfterNow }, NOW)).toBe("active");
  });

  it("classifies the same token differently as `now` advances past its stamp", () => {
    const token = { revoked_at: null, expires_at: "2026-06-15T00:00:00.000Z" };
    // Same input, two instants — proves `now` is actually consulted rather than
    // the function reading a clock of its own.
    expect(tokenStatus(token, new Date("2026-06-14T23:59:59.000Z"))).toBe("active");
    expect(tokenStatus(token, new Date("2026-06-15T00:00:01.000Z"))).toBe("expired");
  });
});

// ── Never-expiring tokens ─────────────────────────────────────────────────────

describe("tokenStatus — a token with no expiry never expires", () => {
  it("reports 'active' for expires_at null", () => {
    expect(tokenStatus({ revoked_at: null, expires_at: null }, NOW)).toBe("active");
  });

  it("reports 'active' when expires_at is absent from the object entirely", () => {
    expect(tokenStatus({ revoked_at: null }, NOW)).toBe("active");
  });

  it("reports 'active' for an empty object — neither field present", () => {
    expect(tokenStatus({}, NOW)).toBe("active");
  });

  it("still reports 'active' at an instant far beyond any plausible expiry", () => {
    // "never" has to mean never, not "not yet". A truthiness bug that read the
    // absent stamp as epoch 0 would call this expired.
    expect(tokenStatus({ expires_at: null }, new Date("2099-01-01T00:00:00.000Z"))).toBe("active");
  });
});

// ── Unparseable stamps ────────────────────────────────────────────────────────

describe("tokenStatus — an unreadable stamp falls back to the API's own filter", () => {
  it("reports 'active' for an unparseable expires_at", () => {
    // A row reached the client through a list whose only filter is
    // `revoked_at IS NULL`, so being present means unrevoked. Nothing about an
    // unreadable stamp shows the expiry "has passed", so the derivation's final
    // `else` applies.
    expect(tokenStatus({ revoked_at: null, expires_at: "not-a-timestamp" }, NOW)).toBe("active");
  });

  it("reports 'active' for an empty-string expires_at", () => {
    expect(tokenStatus({ revoked_at: null, expires_at: "" }, NOW)).toBe("active");
  });

  it("still reports 'revoked' when the stamp is unreadable but the token was withdrawn", () => {
    expect(
      tokenStatus({ revoked_at: "2026-04-01T00:00:00.000Z", expires_at: "not-a-timestamp" }, NOW),
    ).toBe("revoked");
  });

  it("never returns a value outside the three states, whatever the stamps say", () => {
    const inputs = [
      {},
      { expires_at: null },
      { expires_at: "garbage" },
      { expires_at: PAST },
      { revoked_at: "2026-01-01T00:00:00.000Z" },
      { revoked_at: "", expires_at: PAST },
    ];
    for (const input of inputs) {
      expect(["active", "expired", "revoked"]).toContain(tokenStatus(input, NOW));
    }
  });
});

// ── Default `now` ─────────────────────────────────────────────────────────────

describe("tokenStatus — the default `now`", () => {
  it("uses the current instant when no `now` is supplied", () => {
    // The badge component calls the single-argument form, so the default has to
    // be the wall clock and not a fixed value.
    const longPast = "2000-01-01T00:00:00.000Z";
    const longFuture = "2099-01-01T00:00:00.000Z";
    expect(tokenStatus({ expires_at: longPast })).toBe("expired");
    expect(tokenStatus({ expires_at: longFuture })).toBe("active");
  });
});

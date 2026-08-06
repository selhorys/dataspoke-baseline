/**
 * Effective state of a long-lived API token, as the admin token views report it.
 *
 * The route's `include_revoked` filter answers only whether a token was
 * withdrawn; expiry is not part of it, so a token past `expires_at` comes back
 * in the default (active-only) list even though authentication rejects it with
 * `401 TOKEN_EXPIRED`. Deriving the third state here keeps the inventory honest
 * about what can actually reach the deployment.
 *
 * Pure — no React imports; safe in any context.
 */

export type TokenStatus = "active" | "expired" | "revoked";

/** The fields a status is derived from; both are optional and nullable. */
export interface TokenStatusFields {
  revoked_at?: string | null;
  expires_at?: string | null;
}

/**
 * Classify a token. Revocation wins over expiry: a withdrawn credential is
 * withdrawn regardless of when its clock would have run out.
 *
 * `now` is injectable so callers can classify against a fixed instant.
 */
export function tokenStatus(token: TokenStatusFields, now: Date = new Date()): TokenStatus {
  if (token.revoked_at) return "revoked";
  if (!token.expires_at) return "active";
  const expiresAt = new Date(token.expires_at).getTime();
  // An unparseable stamp says nothing about expiry, so the token keeps the
  // state the API's own filter put it in.
  if (Number.isNaN(expiresAt)) return "active";
  return expiresAt <= now.getTime() ? "expired" : "active";
}

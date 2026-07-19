/**
 * Client-side safety checks for operator-supplied display URLs.
 *
 * Mirrors `SAFE_DISPLAY_URL_PATTERN` / `SAFE_PROJECT_ID_PATTERN` in
 * `src/api/schemas/common.py`. The backend checks these values at both the
 * write boundary (the admin PATCH schema) and the read boundary (the
 * peripheral-links router), but the frontend interpolates them straight into an
 * anchor `href`, so it re-checks rather than trusting the transport. React's
 * built-in `javascript:`-URL warning is a diagnostic, not a security control,
 * and it does not cover userinfo spoofing or bidi-disguised hostnames.
 *
 * Barred anywhere in a URL: whitespace, C0 controls (CR/LF header splitting),
 * and the unicode bidi-override set (which can visually disguise a hostname).
 * The authority admits a host plus an optional numeric port and nothing else,
 * so a credential-shaped prefix cannot mask the effective host
 * (`https://trusted.example.com@evil.com`).
 */

// U+0085 is listed explicitly because ECMAScript's `\s` follows the `WhiteSpace`
// production, which excludes NEL, while Python's `\s` follows the Unicode
// White_Space property, which includes it. The backend guard in
// `src/api/schemas/common.py` mirrors this class and bars U+FEFF explicitly for
// the converse reason. Both sides must bar both characters or the two engines
// disagree on the same value; `tests/fixtures/safe-url-cases.json` pins the
// agreement.
const BARRED = "\\s\\u0000-\\u001f\\u0085\\u200e\\u200f\\u202a-\\u202e\\u2066-\\u2069";

/** Admits only http/https — or `""`, meaning "unset, render no link". */
export const SAFE_DISPLAY_URL_RE = new RegExp(
  `^$|^https?:\\/\\/[^${BARRED}:\\/?#@]+(?::[0-9]+)?(?:\\/[^${BARRED}]*)?$`,
);

export const SAFE_DISPLAY_URL_MAX_LENGTH = 512;

/** Langfuse project ids are opaque slugs interpolated into a deep-link path. */
export const SAFE_PROJECT_ID_RE = /^$|^[A-Za-z0-9][A-Za-z0-9_-]*$/;

export const SAFE_PROJECT_ID_MAX_LENGTH = 256;

/**
 * Returns `value` when it is a safe display URL, else `""`.
 *
 * Degrading to `""` rather than throwing keeps the failure mode identical to an
 * unconfigured peripheral: the caller renders no link.
 */
export function sanitizeDisplayUrl(value: string | null | undefined): string {
  if (!value) return "";
  if (value.length > SAFE_DISPLAY_URL_MAX_LENGTH) return "";
  return SAFE_DISPLAY_URL_RE.test(value) ? value : "";
}

/** Returns `value` when it is a safe project-id slug, else `""`. */
export function sanitizeProjectId(value: string | null | undefined): string {
  if (!value) return "";
  if (value.length > SAFE_PROJECT_ID_MAX_LENGTH) return "";
  return SAFE_PROJECT_ID_RE.test(value) ? value : "";
}

/**
 * splitList — the textarea-to-array parser for URN list inputs.
 *
 * Newline is the only separator: one URN per line, each line edge-trimmed, blank
 * lines dropped; whitespace inside a line is preserved. Commas are **not**
 * separators — a dataset URN always contains them inside its
 * `(platform,name,fabric)` tuple, and tag / glossary-term URNs embed a
 * user-authored name that may contain one.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Components (RunDialog).
 */

export function splitList(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

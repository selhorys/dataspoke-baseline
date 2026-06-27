/**
 * Pure helpers for the validation score display.
 *
 * Spec: spec/feature/VALIDATION.md §Validation Result:
 *   "result.type: SUCCESS if score == 1.0 else FAILURE"
 *   "score: 0.0 ≤ score ≤ 1.0"
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §Page contracts:
 *   List shows Quality Score column — "—" until first result row arrives.
 *   Detail header shows latest score badge.
 */

export type BadgeVariant = "success" | "destructive" | "outline";

/**
 * Maps a nullable score value to the Badge variant to render.
 *
 *   score === 1.0   → "success"     (pass / SUCCESS — green)
 *   0 ≤ score < 1  → "destructive"  (fail / FAILURE — red)
 *   null/undefined  → "outline"      (no data — neutral, display "—")
 *
 * The 1.0 boundary mirrors the backend assertionRunEvent mapping:
 *   result.type = SUCCESS if score == 1.0 else FAILURE
 */
export function scoreBadgeVariant(score: number | null | undefined): BadgeVariant {
  if (score === null || score === undefined) return "outline";
  if (score === 1.0) return "success";
  return "destructive";
}

/**
 * Returns the display label for a score value.
 *
 *   number  → score.toFixed(4)  (matches the UI rendering)
 *   null    → "—"
 */
export function scoreLabel(score: number | null | undefined): string {
  if (score === null || score === undefined) return "—";
  return score.toFixed(4);
}

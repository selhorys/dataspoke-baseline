/**
 * Pure logic extracted from ValidationVariablesChart for testability.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §Page contracts:
 *   "Checkbox legend allows toggling visibility of each variable's series."
 *   F6 fix: newly-added variables become visible; at least one series always visible.
 */

/**
 * Toggles a key's presence in the visible set.
 *
 * Invariant: at least one key must always remain visible.
 * If the key is currently the only visible one, the toggle is a no-op.
 */
export function toggleVisibleKey(prev: Set<string>, key: string): Set<string> {
  const next = new Set(prev);
  if (next.has(key)) {
    // Keep at least one visible.
    if (next.size > 1) next.delete(key);
  } else {
    next.add(key);
  }
  return next;
}

/**
 * Syncs the visible-key set when derived keys change (e.g. after conf edit + refetch).
 *
 * Contract:
 *   - New keys in derivedKeys that are not in prev are added (made visible by default).
 *   - Keys already in prev retain their current visibility (existing manual toggles preserved).
 *   - Returns prev unchanged if no new keys are present (referential stability for useEffect).
 */
export function syncVisibleKeys(
  prev: Set<string>,
  derivedKeys: string[],
): Set<string> {
  const hasNew = derivedKeys.some((k) => !prev.has(k));
  if (!hasNew) return prev;
  const next = new Set(prev);
  derivedKeys.forEach((k) => next.add(k));
  return next;
}

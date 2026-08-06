"use client";

import { Badge } from "@/components/ui/badge";
import { formatDateTime } from "@/lib/format-time";
import type { TzMode } from "@/lib/range";
import { tokenStatus, type TokenStatusFields } from "@/lib/token-status";

const VARIANT = {
  active: "success",
  expired: "warning",
  revoked: "secondary",
} as const;

/**
 * The Status cell of the admin token views: active / expired / revoked.
 *
 * The badge stays one word; the stamp behind the state rides along in the
 * `title` for a pointer and in a visually-hidden span for a screen reader,
 * since `Badge` is a plain `<div>` and a `title` alone is announced
 * inconsistently — and the drawer has no Expires column to read it from.
 */
export function TokenStatusBadge({ token, tz }: { token: TokenStatusFields; tz: TzMode }) {
  const status = tokenStatus(token);
  const stamp =
    status === "revoked"
      ? `Revoked ${formatDateTime(token.revoked_at, tz)}`
      : status === "expired"
        ? `Expired ${formatDateTime(token.expires_at, tz)}`
        : undefined;

  return (
    <Badge variant={VARIANT[status]} title={stamp} data-testid="token-status" data-status={status}>
      {status}
      {stamp && <span className="sr-only"> — {stamp}</span>}
    </Badge>
  );
}

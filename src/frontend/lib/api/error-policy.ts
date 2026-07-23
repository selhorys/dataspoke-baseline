/**
 * Single interpretation of a failed read, shared by the global retry rule and
 * the inline render point, so the two cannot drift apart.
 *
 * See spec/feature/FRONTEND_BASIC.md §Query Error Policy.
 */

import { ApiError } from "@/lib/api/client";

/**
 * 503 that reports a configuration state rather than a fault: the peripheral
 * stays unconfigured until an operator wires it.
 */
export const PERIPHERAL_NOT_CONFIGURED = "PERIPHERAL_NOT_CONFIGURED";

/** Display labels for the peripherals the API can name in `detail.peripheral`. */
const PERIPHERAL_LABELS: Record<string, string> = {
  datahub: "DataHub",
  smtp: "SMTP",
};

/** Stands in when the envelope carries no usable `detail.peripheral`. */
const UNNAMED_PERIPHERAL_LABEL = "A required peripheral";

/**
 * True for PERIPHERAL_NOT_CONFIGURED. Every branch that treats this code
 * specially — retry, inline render, toast — keys on the code alone, so an
 * envelope with a missing or malformed `detail` is still classified alike.
 */
export function isPeripheralNotConfigured(error: unknown): boolean {
  return error instanceof ApiError && error.error_code === PERIPHERAL_NOT_CONFIGURED;
}

/**
 * True when retrying cannot change the answer: any 4xx, plus
 * PERIPHERAL_NOT_CONFIGURED regardless of its 503 status. Anything else —
 * including network throwables that are not ApiError — is transient.
 */
export function isNonTransient(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  if (isPeripheralNotConfigured(error)) return true;
  return error.status >= 400 && error.status < 500;
}

/** Global `retry` for every query: fail fast on non-transient, else two retries. */
export function defaultQueryRetry(failureCount: number, error: unknown): boolean {
  if (isNonTransient(error)) return false;
  return failureCount < 2;
}

/**
 * The peripheral named by a PERIPHERAL_NOT_CONFIGURED error (`"datahub"`,
 * `"smtp"`), or null for any other error and for an envelope whose `detail`
 * does not carry the name.
 */
export function unconfiguredPeripheral(error: unknown): string | null {
  if (!(error instanceof ApiError) || !isPeripheralNotConfigured(error)) return null;
  const peripheral = error.detail?.peripheral;
  return typeof peripheral === "string" ? peripheral : null;
}

/**
 * Human-readable subject for a PERIPHERAL_NOT_CONFIGURED message: the mapped
 * label, the raw identifier for a peripheral this client does not know, or a
 * generic stand-in when the envelope names none.
 */
export function peripheralDisplayName(error: unknown): string {
  const peripheral = unconfiguredPeripheral(error);
  if (!peripheral) return UNNAMED_PERIPHERAL_LABEL;
  return PERIPHERAL_LABELS[peripheral] ?? peripheral;
}

/**
 * Global fallback error-toast helper for unhandled API errors.
 *
 * Usage: called from QueryCache/MutationCache onError when a query or
 * mutation does not have meta.handledInline set.
 *
 * Suppresses 401 (auth client clears state and AuthGuard redirects).
 * Handles ApiError (structured API response) and raw network errors
 * (TypeError: Failed to fetch, etc.) with distinct messages.
 */

import { toast } from "@/components/ui/use-toast";
import { ApiError } from "@/lib/api/client";
import { isPeripheralNotConfigured, peripheralDisplayName } from "@/lib/api/error-policy";

export function toastApiError(err: unknown): void {
  if (!(err instanceof ApiError)) {
    // Network errors (TypeError: Failed to fetch) or unknown throwables.
    const isNetworkError =
      err instanceof TypeError &&
      (err.message === "Failed to fetch" || err.message.startsWith("NetworkError"));
    const title = isNetworkError ? "Network error" : "Unexpected error";
    const description = isNetworkError
      ? "Could not reach the server. Check your connection."
      : err instanceof Error
        ? err.message
        : "An unexpected error occurred.";
    toast({ title, description, variant: "destructive" });
    return;
  }

  // 401: auth client already clears state and redirects; skip toast
  if (err.status === 401) return;

  // An unconfigured peripheral names an unfinished setup step rather than a
  // fault, so it toasts neutrally — but it still toasts, because the call it
  // blocked did not happen.
  if (isPeripheralNotConfigured(err)) {
    toast({
      title: `${peripheralDisplayName(err)} isn't connected yet`,
      description: "Connect it in Admin → Peripherals, then try again.",
      variant: "default",
    });
    return;
  }

  toast({
    title: err.error_code,
    description: err.trace_id
      ? `${err.message} (trace: ${err.trace_id.slice(0, 8)})`
      : err.message,
    variant: "destructive",
  });
}

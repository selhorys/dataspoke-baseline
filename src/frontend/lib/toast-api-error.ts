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

  toast({
    title: err.error_code,
    description: err.trace_id
      ? `${err.message} (trace: ${err.trace_id.slice(0, 8)})`
      : err.message,
    variant: "destructive",
  });
}

"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { useEffect, useState } from "react";
import { Toaster } from "@/components/ui/toaster";
import { toastApiError } from "@/lib/toast-api-error";
import { TimezoneHydration } from "@/lib/preferences/timezone";

/**
 * Runs once on mount. If a token is already in the store (e.g. tab was never
 * closed), marks auth as initialized immediately. Otherwise fires the shared
 * refresh probe (same dedup promise used by the 401 interceptor) and marks
 * initialized after it settles — success or failure.
 */
function SilentRefresh() {
  const [ran, setRan] = useState(false);

  useEffect(() => {
    if (ran) return;
    setRan(true);

    void (async () => {
      const { useAuthStore } = await import("@/lib/auth/store");
      const store = useAuthStore.getState();

      if (store.accessToken) {
        store.setAuthInitialized(true);
        return;
      }

      const { ensureFreshToken } = await import("@/lib/api/client");
      await ensureFreshToken();
      useAuthStore.getState().setAuthInitialized(true);
    })();
  }, [ran]);

  return null;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
          },
        },
        /**
         * Global fallback handlers for UNHANDLED errors.
         *
         * Queries and mutations that handle errors inline (page-level toasts,
         * form field errors, or inline error text) opt out by setting
         * meta: { handledInline: true }. For all others, toastApiError is
         * called unconditionally — it suppresses 401s internally and handles
         * both ApiError and raw network errors (TypeError etc.).
         */
        queryCache: new QueryCache({
          onError: (error, query) => {
            if (query.meta?.handledInline) return;
            toastApiError(error);
          },
        }),
        mutationCache: new MutationCache({
          onError: (error, _vars, _ctx, mutation) => {
            if (mutation.meta?.handledInline) return;
            toastApiError(error);
          },
        }),
      }),
  );

  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
      <QueryClientProvider client={queryClient}>
        <SilentRefresh />
        <TimezoneHydration />
        {children}
        <Toaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}

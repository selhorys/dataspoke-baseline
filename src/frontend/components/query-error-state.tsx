"use client";

import Link from "next/link";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { isPeripheralNotConfigured, peripheralDisplayName } from "@/lib/api/error-policy";
import { useAuthStore } from "@/lib/auth/store";
import { cn } from "@/lib/utils";

interface QueryErrorStateProps {
  /** The `error` a failed query surfaced. */
  error: unknown;
  /** What the read was for, e.g. "Failed to load metrics". */
  context: string;
  /** Overrides the composed `${context}: ${message}` copy. */
  message?: string;
  className?: string;
}

/**
 * The single inline render point for a failed read
 * (spec/feature/FRONTEND_BASIC.md §Query Error Policy).
 *
 * An unconfigured peripheral is a setup step the deployment has not reached, so
 * it renders as a muted onboarding state rather than the destructive error
 * state every other failure gets.
 */
export function QueryErrorState({ error, context, message, className }: QueryErrorStateProps) {
  // Role is read straight from the auth store rather than through useMe(): one
  // boolean does not warrant a query, and this renders inside panels that carry
  // no QueryClientProvider of their own. AppShell's useMe() writes `me` into the
  // store on every (app) route, so the role is present wherever this can render.
  // Hooks run on every render, so this sits above the branch below.
  const role = useAuthStore((s) => s.me?.role);

  if (isPeripheralNotConfigured(error)) {
    return (
      <div
        className={cn(
          "flex min-h-[80px] flex-col items-center justify-center gap-2 py-4 text-sm text-muted-foreground",
          className,
        )}
      >
        <p className="font-medium">{peripheralDisplayName(error)} isn&apos;t connected yet</p>
        {/* Before the role resolves only the heading shows: routing an admin to
            "ask an administrator" would misdirect them off the page that fixes
            this. */}
        {role === "Admin" ? (
          <>
            <p>Connect it in Admin → Peripherals to use this page.</p>
            <Button variant="outline" size="sm" asChild>
              <Link href="/admin/peripherals">Go to Peripherals</Link>
            </Button>
          </>
        ) : role ? (
          <p>Ask an administrator to connect it in Admin → Peripherals.</p>
        ) : null}
      </div>
    );
  }

  const errorMessage = error instanceof Error ? error.message : "unknown error";
  return <ErrorState className={className} message={message ?? `${context}: ${errorMessage}`} />;
}

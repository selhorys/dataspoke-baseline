"use client";

import { Suspense } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

/**
 * Landing page for a failed Google sign-in.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §OAuth error page (`/oauth-error`).
 * The API's Google routes are browser-navigation endpoints that 302 on every
 * outcome (spec/API.md §OAuth browser-redirect contract), so a failure arrives
 * here as `?error=<code>` instead of on a JSON envelope. The page is public,
 * makes no API call, and selects copy by lookup into the fixed map below — the
 * received parameter value is never echoed into the rendered output, since the
 * page is directly navigable with any value.
 */

type OAuthErrorCopy = {
  /** Card heading. */
  title: string;
  /** Leading sentence explaining the failure. */
  description: string;
  /** Ordered recovery sequence, for the one code the user cannot leave unaided. */
  steps?: readonly string[];
};

const FALLBACK_COPY: OAuthErrorCopy = {
  title: "Sign-in failed",
  description:
    "Google sign-in could not be completed. Return to the sign-in page and try again, or sign in with your email and password.",
};

// A `Map` rather than an object literal: its keys are exactly the entries below,
// so inherited member names (`toString`, `constructor`, `__proto__`, …) arriving
// as `?error=` cannot resolve to anything, and `.get()` reports every
// unrecognised value as `undefined` without a prototype-key guard.
const OAUTH_ERROR_COPY = new Map<string, OAuthErrorCopy>([
  // spec/feature/AUTH.md §Admin unbind — the steady state of a re-issued
  // address, which the holder cannot resolve without an admin.
  [
    "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT",
    {
      title: "This address is linked to a different Google account",
      description:
        "A DataSpoke account already exists for this email address and is linked to a different Google account. DataSpoke never re-links an account silently, so releasing the old link takes three steps:",
      steps: [
        "Request a password reset for this address and complete it from your inbox — this proves you control the mailbox and gives the account a password.",
        "Ask a DataSpoke administrator to unlink the old Google account from it.",
        "Sign in with Google again. The account links to your current Google identity.",
      ],
    },
  ],
  [
    "GOOGLE_ACCOUNT_LINKED_ELSEWHERE",
    {
      title: "This Google account is already in use",
      description:
        "This Google account is already linked to another DataSpoke user. Return to the sign-in page and try again; if it keeps failing, contact an administrator.",
    },
  ],
  [
    "OAUTH_STATE_MISMATCH",
    {
      title: "Sign-in attempt expired",
      description:
        "The sign-in attempt expired or was interrupted before it completed. Start again from the sign-in page.",
    },
  ],
  [
    "OAUTH_EMAIL_NOT_VERIFIED",
    {
      title: "Email address not verified",
      description:
        "Google has not verified the email address on this account. Verify the address with Google, then try signing in again.",
    },
  ],
  [
    "OAUTH_NOT_CONFIGURED",
    {
      title: "Google sign-in is not available",
      description:
        "Google sign-in is not configured on this deployment. Sign in with your email and password, or contact an administrator.",
    },
  ],
]);

function OAuthErrorCard() {
  const searchParams = useSearchParams();
  const code = searchParams.get("error");
  const copy = (code === null ? undefined : OAUTH_ERROR_COPY.get(code)) ?? FALLBACK_COPY;

  return (
    <div className="rounded-lg border bg-card p-8 shadow-sm">
      <h1 className="mb-4 text-2xl font-semibold tracking-tight">{copy.title}</h1>
      <p className="text-sm text-muted-foreground">{copy.description}</p>

      {copy.steps ? (
        <ol className="mt-4 list-decimal space-y-2 pl-5 text-sm text-muted-foreground">
          {copy.steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      ) : null}

      <div className="mt-6 text-center text-sm">
        <Link href="/login" className="text-primary underline-offset-4 hover:underline">
          Back to sign in
        </Link>
      </div>
    </div>
  );
}

export default function OAuthErrorPage() {
  return (
    <Suspense fallback={<div className="rounded-lg border bg-card p-8 shadow-sm" />}>
      <OAuthErrorCard />
    </Suspense>
  );
}

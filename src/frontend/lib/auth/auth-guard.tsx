"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuthStore } from "./store";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const authInitialized = useAuthStore((s) => s.authInitialized);
  const accessToken = useAuthStore((s) => s.accessToken);

  useEffect(() => {
    if (!authInitialized) return;
    if (!accessToken) {
      const next = encodeURIComponent(pathname);
      router.replace(`/login?next=${next}`);
    }
  }, [authInitialized, accessToken, pathname, router]);

  // Wait for the silent-refresh probe to complete before deciding.
  if (!authInitialized) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-muted border-t-primary" />
      </div>
    );
  }

  if (!accessToken) {
    return null;
  }

  return <>{children}</>;
}

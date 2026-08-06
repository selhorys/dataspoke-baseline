"use client";

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
import { useAuthStore } from "@/lib/auth/store";
import { getRuntimeConfig } from "@/lib/runtime-config";
import type {
  ApiTokenItem,
  ApiTokenListResponse,
  ApiTokenMintResponse,
  Me,
  TokenResponse,
} from "@/lib/api/types";

// ── Login ──────────────────────────────────────────────────────────────────────

interface LoginVars {
  email: string;
  password: string;
}

/**
 * Drop every cached response before a new session begins.
 *
 * The `QueryClient` is created once in `app/providers.tsx` and outlives the
 * client-side navigation between `/login` and the app, so without this the
 * incoming user reads the previous one's cache: `["auth","me"]` still resolves
 * to the outgoing user (and, being fresh, does not refetch), which in turn
 * makes every role-derived gate and every caller-scoped query key report the
 * outgoing identity. Runs before the caller stores the access token, so the new
 * session's own reads — all gated on that token — start against an empty cache.
 */
function resetSessionCache(qc: QueryClient): void {
  qc.removeQueries();
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation<TokenResponse, Error, LoginVars>({
    mutationFn: (vars) =>
      apiFetch<TokenResponse>("/auth/token", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      resetSessionCache(qc);
    },
  });
}

// ── Register ───────────────────────────────────────────────────────────────────

interface RegisterVars {
  email: string;
  name: string;
  password: string;
}

export function useRegister() {
  const qc = useQueryClient();
  return useMutation<TokenResponse, Error, RegisterVars>({
    mutationFn: (vars) =>
      apiFetch<TokenResponse>("/auth/register", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      resetSessionCache(qc);
    },
  });
}

// ── Password reset ─────────────────────────────────────────────────────────────

export function useRequestPasswordReset() {
  return useMutation<void, Error, { email: string }>({
    mutationFn: (vars) =>
      apiFetch<void>("/auth/password/reset/request", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    meta: { handledInline: true },
  });
}

export function useConfirmPasswordReset() {
  return useMutation<void, Error, { token: string; new_password: string }>({
    mutationFn: (vars) =>
      apiFetch<void>("/auth/password/reset/confirm", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    meta: { handledInline: true },
  });
}

// ── Profile ────────────────────────────────────────────────────────────────────

export function useUpdateProfile() {
  const qc = useQueryClient();
  return useMutation<Me, Error, { name?: string; password?: string }>({
    mutationFn: (vars) =>
      apiFetch<Me>("/auth/me", {
        method: "PATCH",
        body: JSON.stringify(vars),
      }),
    meta: { handledInline: true },
    onSuccess: (data) => {
      qc.setQueryData(["auth", "me"], data);
    },
  });
}

// ── API tokens ─────────────────────────────────────────────────────────────────

/**
 * Invalidate every cache that can be showing a given user's tokens: the
 * self-scoped list, the deployment-wide admin inventory, and that user's row
 * drawer under Admin → Users. The last two exist only in an Admin session;
 * invalidating an absent key is a no-op.
 *
 * Shared by the self-service mint/revoke here and the admin revoke in
 * `lib/api/admin.ts`, which reach the same three caches from opposite ends.
 */
export function invalidateTokenReads(qc: QueryClient, ownerId: string | undefined): void {
  void qc.invalidateQueries({ queryKey: ["auth", "api-tokens"] });
  void qc.invalidateQueries({ queryKey: ["admin", "api-tokens"] });
  if (ownerId) {
    void qc.invalidateQueries({ queryKey: ["admin", "users", ownerId, "api-tokens"] });
  }
}

export function useApiTokens() {
  return useQuery<ApiTokenListResponse>({
    queryKey: ["auth", "api-tokens"],
    queryFn: () => apiFetch<ApiTokenListResponse>("/auth/api-tokens"),
    meta: { handledInline: true },
  });
}

interface MintTokenVars {
  name: string;
  expires_at?: string | null;
}

export function useCreateApiToken() {
  const qc = useQueryClient();
  const meId = useAuthStore((s) => s.me?.id);
  return useMutation<ApiTokenMintResponse, Error, MintTokenVars>({
    mutationFn: (vars) =>
      apiFetch<ApiTokenMintResponse>("/auth/api-tokens", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      invalidateTokenReads(qc, meId);
    },
  });
}

export function useDeleteApiToken() {
  const qc = useQueryClient();
  const meId = useAuthStore((s) => s.me?.id);
  return useMutation<void, Error, { id: string }>({
    mutationFn: ({ id }) =>
      apiFetch<void>(`/auth/api-tokens/${id}`, { method: "DELETE" }),
    meta: { handledInline: true },
    onSuccess: () => {
      invalidateTokenReads(qc, meId);
    },
  });
}

// ── Google OAuth ───────────────────────────────────────────────────────────────
// The Google login button navigates the browser to /auth/google/login.
// The server handles the redirect; we only need the URL — no fetch hook.

export function getGoogleLoginUrl(): string {
  const base = getRuntimeConfig().apiBaseUrl + "/api/v1";
  return `${base}/auth/google/login`;
}

// Convenience accessor for unused imports (type-level)
export type { ApiTokenItem };

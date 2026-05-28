"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
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

export function useLogin() {
  return useMutation<TokenResponse, Error, LoginVars>({
    mutationFn: (vars) =>
      apiFetch<TokenResponse>("/auth/token", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    meta: { handledInline: true },
  });
}

// ── Register ───────────────────────────────────────────────────────────────────

interface RegisterVars {
  email: string;
  name: string;
  password: string;
}

export function useRegister() {
  return useMutation<TokenResponse, Error, RegisterVars>({
    mutationFn: (vars) =>
      apiFetch<TokenResponse>("/auth/register", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    meta: { handledInline: true },
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
  return useMutation<ApiTokenMintResponse, Error, MintTokenVars>({
    mutationFn: (vars) =>
      apiFetch<ApiTokenMintResponse>("/auth/api-tokens", {
        method: "POST",
        body: JSON.stringify(vars),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["auth", "api-tokens"] });
    },
  });
}

export function useDeleteApiToken() {
  const qc = useQueryClient();
  return useMutation<void, Error, { id: string }>({
    mutationFn: ({ id }) =>
      apiFetch<void>(`/auth/api-tokens/${id}`, { method: "DELETE" }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["auth", "api-tokens"] });
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

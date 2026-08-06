"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
import { invalidateTokenReads } from "@/lib/api/auth";
import type {
  AdminApiTokenListResponse,
  AdminUser,
  ApiTokenItem,
  DagGroup,
  DagGroupPatch,
  DagGroupStatus,
  DagGroupsResponse,
  DatahubPeripheral,
  DatahubPeripheralPatch,
  LangfusePeripheral,
  LangfusePeripheralPatch,
  RuntimeConf,
  RuntimeConfPatch,
  UsersListResponse,
  UserRole,
} from "@/lib/api/types";

// ── Users list ─────────────────────────────────────────────────────────────────

interface ListUsersParams {
  offset?: number;
  limit?: number;
}

export function useAdminUsers(params: ListUsersParams = {}) {
  const { offset = 0, limit = 100 } = params;
  return useQuery<UsersListResponse>({
    queryKey: ["admin", "users", offset, limit],
    queryFn: () =>
      apiFetch<UsersListResponse>(`/admin/users?offset=${offset}&limit=${limit}`),
    meta: { handledInline: true },
  });
}

// ── Update user name ───────────────────────────────────────────────────────────

export function useUpdateUserName() {
  const qc = useQueryClient();
  return useMutation<AdminUser, Error, { id: string; name: string }>({
    mutationFn: ({ id, name }) =>
      apiFetch<AdminUser>(`/admin/users/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "users"] });
    },
  });
}

// ── Update user role ───────────────────────────────────────────────────────────

export function useUpdateUserRole() {
  const qc = useQueryClient();
  return useMutation<{ role: UserRole }, Error, { id: string; role: UserRole }>({
    mutationFn: ({ id, role }) =>
      apiFetch<{ role: UserRole }>(`/admin/users/${id}/role`, {
        method: "PATCH",
        body: JSON.stringify({ role }),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "users"] });
    },
  });
}

// ── Delete user ────────────────────────────────────────────────────────────────

export function useDeleteUser() {
  const qc = useQueryClient();
  return useMutation<void, Error, { id: string }>({
    mutationFn: ({ id }) =>
      apiFetch<void>(`/admin/users/${id}`, { method: "DELETE" }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "users"] });
    },
  });
}

// ── Unlink Google binding ──────────────────────────────────────────────────────

export function useUnlinkUserGoogle() {
  const qc = useQueryClient();
  return useMutation<void, Error, { id: string }>({
    mutationFn: ({ id }) =>
      apiFetch<void>(`/admin/users/${id}/google`, { method: "DELETE" }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "users"] });
    },
  });
}

// ── API tokens (admin views) ───────────────────────────────────────────────────

interface AdminUserTokensParams {
  /** Include rows with `revoked_at` set. Off by default, matching the route. */
  includeRevoked?: boolean;
}

/**
 * One user's tokens, reached from that user's row. Unpaged: the window is one
 * screenful wide by construction (a user holds at most 10 active tokens), and
 * the caller discloses any overflow from `total_count`.
 *
 * The key names the user being read, not the caller reading them. Adding the
 * caller would buy nothing here: the caller's id is itself read from
 * `["auth","me"]`, so two sessions sharing a stale `me` would share the scoped
 * key too. What separates them is the cache purge on sign-out and sign-in
 * (`components/app-shell.tsx`, `lib/api/auth.ts`), which this key relies on
 * exactly as `["admin","users",…]` beside it does.
 */
export function useAdminUserTokens(userId: string, params: AdminUserTokensParams = {}) {
  const offset = 0;
  const limit = 100;
  const { includeRevoked = false } = params;
  const query = `offset=${offset}&limit=${limit}&include_revoked=${includeRevoked}`;
  return useQuery<AdminApiTokenListResponse>({
    // The varying params sit in the key so a toggle refetches rather than
    // serving the previous scope's rows.
    queryKey: ["admin", "users", userId, "api-tokens", { offset, limit, includeRevoked }],
    queryFn: () =>
      apiFetch<AdminApiTokenListResponse>(`/admin/users/${userId}/api-tokens?${query}`),
    enabled: !!userId,
    meta: { handledInline: true },
  });
}

interface AdminApiTokensParams {
  /**
   * The signed-in user's id, carried in the query key so two sessions in one
   * tab cannot share a cache entry. It is defence in depth, not the fix for a
   * cross-session paint: the id is itself read from `["auth","me"]`, so it only
   * separates the two once that entry has been replaced. The cache purge on
   * sign-out and sign-in (`components/app-shell.tsx`, `lib/api/auth.ts`) is
   * what actually ends the previous session's data.
   */
  callerId: string | undefined;
  /** The page window. Required — a paged view owns its own offset/limit. */
  offset: number;
  limit: number;
  /** Include rows with `revoked_at` set. Off by default, matching the route. */
  includeRevoked?: boolean;
  enabled?: boolean;
}

/**
 * The deployment-wide token inventory.
 *
 * The route is Admin-only and `require_admin` is where that is enforced; this
 * `enabled` gate is a client-side courtesy that keeps a session the client does
 * not believe to be Admin — or one whose identity has not resolved — from
 * issuing a request that would 403.
 */
export function useAdminApiTokens(params: AdminApiTokensParams) {
  const { callerId, offset, limit, includeRevoked = false, enabled = true } = params;
  const query = `offset=${offset}&limit=${limit}&include_revoked=${includeRevoked}`;
  return useQuery<AdminApiTokenListResponse>({
    queryKey: ["admin", "api-tokens", callerId, { offset, limit, includeRevoked }],
    queryFn: () => apiFetch<AdminApiTokenListResponse>(`/admin/api-tokens?${query}`),
    enabled: enabled && !!callerId,
    meta: { handledInline: true },
  });
}

export function useDeleteAdminUserToken() {
  const qc = useQueryClient();
  return useMutation<void, Error, { userId: string; tokenId: string }>({
    mutationFn: ({ userId, tokenId }) =>
      apiFetch<void>(`/admin/users/${userId}/api-tokens/${tokenId}`, {
        method: "DELETE",
      }),
    meta: { handledInline: true },
    // The revoked row can be showing in the per-user drawer, the cross-user
    // inventory, and — when the owner is the caller — their own list.
    onSuccess: (_data, vars) => {
      invalidateTokenReads(qc, vars.userId);
    },
  });
}

export type { ApiTokenItem };

// ── Runtime configuration ──────────────────────────────────────────────────────

export function useRuntimeConf() {
  return useQuery<RuntimeConf>({
    queryKey: ["admin", "conf"],
    queryFn: () => apiFetch<RuntimeConf>("/admin/conf"),
    meta: { handledInline: true },
  });
}

export function useUpdateRuntimeConf() {
  const qc = useQueryClient();
  return useMutation<RuntimeConf, Error, RuntimeConfPatch>({
    mutationFn: (body) =>
      apiFetch<RuntimeConf>("/admin/conf", {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "conf"] });
    },
  });
}

// ── Workflow schedules (DAG groups) ──────────────────────────────────────────

export function useDagGroups() {
  return useQuery<DagGroupsResponse>({
    queryKey: ["admin", "dags"],
    queryFn: () => apiFetch<DagGroupsResponse>("/admin/dags"),
    meta: { handledInline: true },
  });
}

export function useSetDagGroupPaused() {
  const qc = useQueryClient();
  return useMutation<DagGroupStatus, Error, { group: DagGroup; paused: boolean }>({
    mutationFn: ({ group, paused }) =>
      apiFetch<DagGroupStatus>(`/admin/dags/${group}`, {
        method: "PATCH",
        body: JSON.stringify({ paused } satisfies DagGroupPatch),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "dags"] });
    },
  });
}

// ── DataHub peripheral ─────────────────────────────────────────────────────────

export function useDatahubPeripheral() {
  return useQuery<DatahubPeripheral>({
    queryKey: ["admin", "peripherals", "datahub"],
    queryFn: () => apiFetch<DatahubPeripheral>("/admin/peripherals/datahub"),
    meta: { handledInline: true },
  });
}

export function useUpdateDatahubPeripheral() {
  const qc = useQueryClient();
  return useMutation<DatahubPeripheral, Error, DatahubPeripheralPatch>({
    mutationFn: (body) =>
      apiFetch<DatahubPeripheral>("/admin/peripherals/datahub", {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "peripherals", "datahub"] });
    },
  });
}

// ── Langfuse peripheral ────────────────────────────────────────────────────────

export function useLangfusePeripheral() {
  return useQuery<LangfusePeripheral>({
    queryKey: ["admin", "peripherals", "langfuse"],
    queryFn: () => apiFetch<LangfusePeripheral>("/admin/peripherals/langfuse"),
    meta: { handledInline: true },
  });
}

export function useUpdateLangfusePeripheral() {
  const qc = useQueryClient();
  return useMutation<LangfusePeripheral, Error, LangfusePeripheralPatch>({
    mutationFn: (body) =>
      apiFetch<LangfusePeripheral>("/admin/peripherals/langfuse", {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "peripherals", "langfuse"] });
    },
  });
}

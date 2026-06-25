"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
import type {
  AdminUser,
  ApiTokenItem,
  ApiTokenListResponse,
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

// ── User API tokens (admin view) ───────────────────────────────────────────────

export function useAdminUserTokens(userId: string) {
  return useQuery<ApiTokenListResponse>({
    queryKey: ["admin", "users", userId, "api-tokens"],
    queryFn: () =>
      apiFetch<ApiTokenListResponse>(`/admin/users/${userId}/api-tokens`),
    enabled: !!userId,
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
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({
        queryKey: ["admin", "users", vars.userId, "api-tokens"],
      });
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

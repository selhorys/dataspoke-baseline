import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { apiFetch } from "@/lib/api/client";
import type { Me } from "@/lib/api/types";
import { useAuthStore } from "./store";

export function useMe() {
  const accessToken = useAuthStore((s) => s.accessToken);
  const setMe = useAuthStore((s) => s.setMe);
  const me = useAuthStore((s) => s.me);

  const query = useQuery<Me>({
    queryKey: ["auth", "me"],
    queryFn: () => apiFetch<Me>("/auth/me"),
    enabled: !!accessToken,
    staleTime: 60_000,
    meta: { handledInline: true },
  });

  useEffect(() => {
    if (query.data) {
      setMe(query.data);
    }
  }, [query.data, setMe]);

  const role = me?.role ?? query.data?.role;

  return {
    me: me ?? query.data ?? null,
    isLoading: query.isLoading,
    isAdmin: role === "Admin",
    isEditor: role === "Editor",
    canWrite: role === "Admin" || role === "Editor",
  };
}

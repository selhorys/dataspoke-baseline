import { create } from "zustand";
import type { Me } from "@/lib/api/types";

interface AuthState {
  accessToken: string | null;
  me: Me | null;
  authInitialized: boolean;
  setToken: (token: string) => void;
  setMe: (me: Me) => void;
  setAuthInitialized: (v: boolean) => void;
  clear: () => void;
}

export const useAuthStore = create<AuthState>()((set) => ({
  accessToken: null,
  me: null,
  authInitialized: false,
  setToken: (token) => set({ accessToken: token }),
  setMe: (me) => set({ me }),
  setAuthInitialized: (v) => set({ authInitialized: v }),
  clear: () => set({ accessToken: null, me: null }),
}));

"use client";

/**
 * Global timezone preference — governs how every timestamp is *displayed*
 * across the app (formatters read it; query bounds are unaffected, see lib/range.ts).
 *
 * Backed by localStorage via zustand's persist middleware. Hydration is
 * deferred (skipHydration: true) so the first client render matches the
 * server-rendered default ("local"), avoiding a hydration mismatch on
 * timestamp text. <TimezoneHydration> rehydrates from storage on mount.
 */

import { useEffect } from "react";
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import type { TzMode } from "@/lib/range";

interface TimezoneState {
  tz: TzMode;
  setTz: (tz: TzMode) => void;
}

export const useTimezoneStore = create<TimezoneState>()(
  persist(
    (set) => ({
      tz: "local",
      setTz: (tz) => set({ tz }),
    }),
    {
      name: "dataspoke:timezone",
      storage: createJSONStorage(() => localStorage),
      // Defer reading storage until <TimezoneHydration> runs on mount, so SSR
      // and the first client render agree on the default.
      skipHydration: true,
    },
  ),
);

/**
 * Mount once (alongside SilentRefresh) to rehydrate the persisted timezone
 * after the first client render. Renders nothing.
 */
export function TimezoneHydration() {
  useEffect(() => {
    void useTimezoneStore.persist.rehydrate();
  }, []);

  return null;
}

/** Selector hook — the active display timezone. Reactive across the app. */
export function useDisplayTz(): TzMode {
  return useTimezoneStore((s) => s.tz);
}

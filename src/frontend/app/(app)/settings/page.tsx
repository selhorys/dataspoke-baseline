"use client";

import { useEffect, useState } from "react";
import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useTimezoneStore } from "@/lib/preferences/timezone";
import type { TzMode } from "@/lib/range";

const LOCALE_KEY = "dataspoke:locale";

type Locale = "en" | "ko";

function useLocale(): [Locale, (l: Locale) => void] {
  const [locale, setLocaleState] = useState<Locale>("en");

  useEffect(() => {
    const stored = localStorage.getItem(LOCALE_KEY);
    if (stored === "en" || stored === "ko") {
      setLocaleState(stored);
    }
  }, []);

  function setLocale(l: Locale) {
    localStorage.setItem(LOCALE_KEY, l);
    setLocaleState(l);
  }

  return [locale, setLocale];
}

export default function SettingsPage() {
  const { theme, setTheme } = useTheme();
  const [locale, setLocale] = useLocale();
  const tz = useTimezoneStore((s) => s.tz);
  const setTz = useTimezoneStore((s) => s.setTz);

  const themes = [
    { value: "light", label: "Light" },
    { value: "dark", label: "Dark" },
    { value: "system", label: "System" },
  ] as const;

  const timezones = [
    { value: "local", label: "Local" },
    { value: "utc", label: "UTC" },
  ] as const;

  return (
    <div className="space-y-8">
      <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>

      <section className="space-y-3">
        <h2 className="text-base font-medium">Theme</h2>
        <p className="text-sm text-muted-foreground">
          Choose your preferred color scheme. Persisted in your browser.
        </p>
        <div className="flex gap-2">
          {themes.map((t) => (
            <Button
              key={t.value}
              variant={theme === t.value ? "default" : "outline"}
              size="sm"
              onClick={() => setTheme(t.value)}
            >
              {t.label}
            </Button>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-base font-medium">Timezone</h2>
        <p className="text-sm text-muted-foreground">
          Timezone for displaying dates and times across the app. Persisted in your browser.
        </p>
        <div className="flex gap-2">
          {timezones.map((t) => (
            <Button
              key={t.value}
              variant={tz === t.value ? "default" : "outline"}
              size="sm"
              onClick={() => setTz(t.value as TzMode)}
            >
              {t.label}
            </Button>
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <h2 className="text-base font-medium">Language</h2>
        <p className="text-sm text-muted-foreground">
          Interface language preference. Persisted in your browser. Translations are not yet wired up.
        </p>
        <Select value={locale} onValueChange={(v) => setLocale(v as Locale)}>
          <SelectTrigger className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="en">English</SelectItem>
            <SelectItem value="ko">Korean</SelectItem>
          </SelectContent>
        </Select>
      </section>
    </div>
  );
}

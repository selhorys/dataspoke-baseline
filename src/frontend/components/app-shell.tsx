"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  BarChart3,
  ChevronDown,
  ChevronRight,
  Database,
  GitBranch,
  LayoutDashboard,
  LogOut,
  Network,
  PackageOpen,
  Scale,
  Settings,
  Shield,
  SlidersHorizontal,
  Sparkles,
  User,
  Users,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/lib/auth/store";
import { useMe } from "@/lib/auth/use-me";
import { ApiError, apiFetch } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ThemeToggle } from "@/components/theme-toggle";
import { NotificationCenter } from "@/components/notification-center";
import { DataHubIcon, AirflowIcon, LangfuseIcon, RedocIcon } from "@/components/brand-icons";
import { getRuntimeConfig } from "@/lib/runtime-config";
import { useDisplayLinks } from "@/lib/api/peripheral-links";

interface NavItem {
  label: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  /** Optional feature-hue tint for the icon (the hub-and-spoke signature). */
  iconClassName?: string;
}

interface NavGroup {
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  basePath: string;
  /** Optional feature-hue tint for the group icon. */
  iconClassName?: string;
  children: NavItem[];
}

const mainNav: (NavItem | NavGroup)[] = [
  {
    label: "Governance",
    icon: Scale,
    iconClassName: "text-feature-governance",
    basePath: "/governance",
    children: [
      { label: "Dashboard", href: "/governance/dashboard", icon: LayoutDashboard },
      { label: "Metrics", href: "/governance/metrics", icon: BarChart3 },
      { label: "Datasets", href: "/governance/datasets", icon: Database },
    ],
  },
  {
    label: "Ingestion",
    icon: Database,
    iconClassName: "text-feature-ingestion",
    basePath: "/ingestion",
    children: [
      { label: "Config", href: "/ingestion/conf", icon: SlidersHorizontal },
      { label: "Unmanaged", href: "/ingestion/unmanaged", icon: PackageOpen },
    ],
  },
  {
    label: "Validation",
    href: "/validation",
    icon: Shield,
    iconClassName: "text-feature-validation",
  },
  {
    label: "OntoGen",
    icon: Network,
    iconClassName: "text-feature-ontogen",
    basePath: "/ontogen",
    children: [
      { label: "Config", href: "/ontogen/conf", icon: Network },
      { label: "Seed", href: "/ontogen/seed", icon: Network },
      { label: "Result", href: "/ontogen/result", icon: Network },
    ],
  },
  {
    label: "MetaGen",
    icon: Sparkles,
    iconClassName: "text-feature-metagen",
    basePath: "/metagen",
    children: [
      { label: "Config", href: "/metagen/conf", icon: SlidersHorizontal },
      { label: "Result", href: "/metagen/result", icon: Sparkles },
      { label: "Uncovered", href: "/metagen/uncovered", icon: PackageOpen },
    ],
  },
];

function isNavGroup(entry: NavItem | NavGroup): entry is NavGroup {
  return "children" in entry;
}

const accountNav: NavItem[] = [
  { label: "Profile", href: "/profile", icon: User },
  { label: "API Tokens", href: "/profile/tokens", icon: GitBranch },
  { label: "Settings", href: "/settings", icon: Settings },
];

const adminNav: NavItem[] = [
  { label: "Users", href: "/admin/users", icon: Users },
  { label: "Configurations", href: "/admin/conf", icon: SlidersHorizontal },
  { label: "Peripherals", href: "/admin/peripherals", icon: Network },
];

function SidebarLink({ item }: { item: NavItem }) {
  const pathname = usePathname();
  const isActive = pathname === item.href || pathname.startsWith(item.href + "/");
  const Icon = item.icon;

  return (
    <Link
      href={item.href}
      className={cn(
        "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
        isActive
          ? "bg-accent text-accent-foreground"
          : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
      )}
    >
      <Icon className={cn("h-4 w-4 shrink-0", item.iconClassName)} />
      {item.label}
    </Link>
  );
}

function SidebarNavGroup({ group }: { group: NavGroup }) {
  const pathname = usePathname();
  const isActive = pathname === group.basePath || pathname.startsWith(group.basePath + "/");
  const [open, setOpen] = useState(isActive);
  const Icon = group.icon;

  useEffect(() => {
    if (isActive) setOpen(true);
  }, [isActive]);

  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          "flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
          isActive
            ? "bg-accent text-accent-foreground"
            : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
        )}
      >
        <Icon className={cn("h-4 w-4 shrink-0", group.iconClassName)} />
        {group.label}
        <Chevron className="ml-auto h-4 w-4 shrink-0" />
      </button>
      {open && (
        <div className="mt-1 flex flex-col gap-1 pl-6">
          {group.children.map((child) => (
            <SidebarLink key={child.href} item={child} />
          ))}
        </div>
      )}
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { me, isAdmin } = useMe();
  const clear = useAuthStore((s) => s.clear);

  /**
   * Fails closed: local state is cleared only once the server confirms the
   * refresh token is revoked. The refresh cookie is httpOnly, so a failed
   * revoke leaves a live server-side session that only the backend can end —
   * clearing and redirecting anyway would show "logged out" over a session
   * that can still be resumed via /auth/token/refresh.
   */
  async function handleLogout() {
    try {
      await apiFetch("/auth/token/revoke", { method: "POST" });
    } catch (err) {
      const trace =
        err instanceof ApiError && err.trace_id ? ` (trace: ${err.trace_id.slice(0, 8)})` : "";
      toast({
        title: "Logout failed",
        description:
          "You are still signed in and your session is still active. Please try again, and " +
          `do not leave this device unattended until logout succeeds.${trace}`,
        variant: "destructive",
      });
      return;
    }
    clear();
    router.replace("/login");
  }

  const displayName = me?.name ?? me?.email ?? "Account";

  // DataHub and Langfuse are externally-wired peripherals: env-first, then
  // GET /spoke/common/peripheral-links. Airflow and ReDoc are deployment-local
  // (Airflow ships in the umbrella chart, ReDoc is the API itself), so they are
  // absent from that endpoint and stay on the runtime config alone.
  const { datahubUrl, langfuseUrl } = useDisplayLinks();
  const { airflowUrl, apiBaseUrl } = getRuntimeConfig();
  const infraLinks = [
    // Link to /login (not the root): the React login page offers both the
    // username/password form and a "Sign in with SSO" button. The root would
    // hit /authenticate and auto-redirect to the OIDC provider for guests.
    { label: "DataHub", href: datahubUrl ? `${datahubUrl}/login` : "", icon: DataHubIcon },
    { label: "Langfuse", href: langfuseUrl, icon: LangfuseIcon },
    { label: "Airflow", href: airflowUrl, icon: AirflowIcon },
    { label: "API docs", href: apiBaseUrl ? `${apiBaseUrl}/redoc` : "", icon: RedocIcon },
  ];

  return (
    <div className="fixed inset-0 flex flex-col">
      {/* Header */}
      <header className="relative flex h-14 shrink-0 items-center overflow-hidden border-b bg-background px-4">
        {/* Traditional Korean meander/lattice pattern; black line-art flipped to light via dark:invert */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 z-0 bg-[url('/brand/menu-pattern.png')] bg-[length:auto_100%] bg-repeat-x opacity-60 dark:opacity-50 dark:invert"
        />
        <Link
          href="/governance/dashboard"
          className="relative z-10 mr-auto text-base font-semibold tracking-tight"
        >
          DataSpoke
        </Link>

        <div className="relative z-10 flex items-center gap-2">
          {infraLinks
            .filter((l) => l.href)
            .map((l) => {
              const Icon = l.icon;
              return (
                <Button
                  key={l.label}
                  asChild
                  variant="ghost"
                  size="icon"
                  title={l.label}
                  aria-label={`Open ${l.label}`}
                >
                  <a href={l.href} target="_blank" rel="noopener noreferrer">
                    <Icon className="h-[18px] w-auto" />
                  </a>
                </Button>
              );
            })}
          <ThemeToggle />
          <NotificationCenter />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="sm" className="gap-2">
                <User className="h-4 w-4" />
                <span className="hidden sm:inline-block">{displayName}</span>
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-48">
              <DropdownMenuLabel className="font-normal">
                <p className="text-sm font-medium">{me?.name}</p>
                <p className="text-xs text-muted-foreground">{me?.email}</p>
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem asChild>
                <Link href="/profile">Profile</Link>
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <Link href="/profile/tokens">API Tokens</Link>
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <Link href="/settings">Settings</Link>
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="text-destructive focus:text-destructive"
                onClick={handleLogout}
              >
                <LogOut className="mr-2 h-4 w-4" />
                Logout
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar */}
        <aside className="hidden w-56 shrink-0 flex-col border-r bg-background md:flex">
          <nav className="flex flex-1 flex-col gap-1 overflow-y-auto min-h-0 p-3">
            {mainNav.map((entry) =>
              isNavGroup(entry) ? (
                <SidebarNavGroup key={entry.basePath} group={entry} />
              ) : (
                <SidebarLink key={entry.href} item={entry} />
              ),
            )}

            <div className="mt-auto border-t pt-3">
              {isAdmin && (
                <div className="mb-3">
                  <p className="mb-1 px-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Admin
                  </p>
                  {adminNav.map((item) => (
                    <SidebarLink key={item.href} item={item} />
                  ))}
                </div>
              )}
              <div>
                <p className="mb-1 px-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Account
                </p>
                {accountNav.map((item) => (
                  <SidebarLink key={item.href} item={item} />
                ))}
              </div>
            </div>
          </nav>
        </aside>

        {/* Main content */}
        <main className="flex-1 overflow-y-auto p-6">{children}</main>
      </div>
    </div>
  );
}

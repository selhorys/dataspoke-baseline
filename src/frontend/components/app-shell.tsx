"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  BarChart3,
  Database,
  GitBranch,
  LayoutDashboard,
  LogOut,
  Network,
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
import { apiFetch } from "@/lib/api/client";
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

interface NavItem {
  label: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
}

const mainNav: NavItem[] = [
  { label: "Dashboard", href: "/governance/dashboard", icon: LayoutDashboard },
  { label: "Metrics", href: "/governance/metrics", icon: BarChart3 },
  { label: "Ingestion", href: "/ingestion", icon: Database },
  { label: "Validation", href: "/validation", icon: Shield },
  { label: "OntoGen", href: "/ontogen", icon: Network },
  { label: "MetaGen", href: "/metagen", icon: Sparkles },
];

const accountNav: NavItem[] = [
  { label: "Profile", href: "/profile", icon: User },
  { label: "API Tokens", href: "/profile/tokens", icon: GitBranch },
  { label: "Settings", href: "/settings", icon: Settings },
];

const adminNav: NavItem[] = [
  { label: "Users", href: "/admin/users", icon: Users },
  { label: "Configurations", href: "/admin/conf", icon: SlidersHorizontal },
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
      <Icon className="h-4 w-4 shrink-0" />
      {item.label}
    </Link>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { me, isAdmin } = useMe();
  const clear = useAuthStore((s) => s.clear);

  async function handleLogout() {
    try {
      await apiFetch("/auth/token/revoke", { method: "POST" });
    } catch {
      // clear the local state regardless of the API call outcome
    } finally {
      clear();
      router.replace("/login");
    }
  }

  const displayName = me?.name ?? me?.email ?? "Account";

  const { datahubUrl, langfuseUrl, airflowUrl, apiBaseUrl } = getRuntimeConfig();
  const infraLinks = [
    { label: "DataHub", href: datahubUrl, icon: DataHubIcon },
    { label: "Langfuse", href: langfuseUrl, icon: LangfuseIcon },
    { label: "Airflow", href: airflowUrl, icon: AirflowIcon },
    { label: "API docs", href: apiBaseUrl ? `${apiBaseUrl}/redoc` : "", icon: RedocIcon },
  ];

  return (
    <div className="fixed inset-0 flex flex-col">
      {/* Header */}
      <header className="flex h-14 shrink-0 items-center border-b bg-background px-4">
        <span className="mr-auto text-base font-semibold tracking-tight">DataSpoke</span>

        <div className="flex items-center gap-2">
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
                    <Icon className="h-[18px] w-[18px]" />
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
          <nav className="flex flex-1 flex-col gap-1 p-3">
            {mainNav.map((item) => (
              <SidebarLink key={item.href} item={item} />
            ))}

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

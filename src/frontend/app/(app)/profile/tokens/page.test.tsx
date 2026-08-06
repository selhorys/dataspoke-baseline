/**
 * Tests for app/(app)/profile/tokens/page.tsx — the API-token page and its
 * Admin-only All-tokens scope.
 *
 * Spec traces (spec/feature/FRONTEND_BASIC.md §API tokens (`/profile/tokens`)):
 *   - "The scope control renders for Admins only; every other role sees the
 *     My-tokens table alone and never issues the admin request."
 *   - "My tokens reads `GET /auth/api-tokens`; All tokens reads
 *     `GET /admin/api-tokens` and adds the Owner and Status columns to the same
 *     set plus a 'Show revoked' checkbox carrying the route's `include_revoked`
 *     param, off by default."
 *   - "Status is one of **active**, **expired**, or **revoked**, derived
 *     client-side… Revocation wins over expiry, being the deliberate act."
 *   - "The All-tokens table composes the shared Pagination control against the
 *     route's `offset`/`limit`/`total_count`… My tokens needs none, bounded by
 *     the 10-active-token-per-user cap."
 *   - "'New token' is hidden in the All-tokens scope: minting is self-only…
 *     Revoke in that scope goes through
 *     `DELETE /admin/users/{id}/api-tokens/{token_id}` addressed by the row's
 *     `user_id`, including for an Admin's own token… a revoked row exposes no
 *     revoke action."
 *   - §Routing, `/profile/tokens`: the route's API set is exactly
 *     `GET|POST /auth/api-tokens`, `DELETE /auth/api-tokens/{id}`,
 *     `GET /admin/api-tokens`, `DELETE /admin/users/{id}/api-tokens/{token_id}`.
 *   - spec/feature/AUTH.md §Revoked-token visibility: both admin reads "exclude
 *     revoked rows by default and take `include_revoked=true` to bring them
 *     back"; expiry is not filtered, so an expired row sits in the default page.
 *
 * The traces above cover sections 1–5. Section 6 (caller-scoped cache) has no
 * spec anchor and says so at the `describe` — see the NOT-A-SPEC-CLAIM note
 * there.
 *
 * Mocking boundary: only `@/lib/api/client` (the fetch), `@/lib/auth/use-me`
 * (the session role) and the toast are mocked. The data hooks in
 * `lib/api/auth.ts` / `lib/api/admin.ts` run for real inside a QueryClient, so
 * "the admin request never fires for a non-admin" is asserted against the actual
 * request the page would put on the wire, not against a stand-in hook that
 * cannot fire one.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AdminApiTokenItem, ApiTokenItem, Me, UserRole } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Browser API stubs — jsdom lacks ResizeObserver / pointer capture /
// scrollIntoView, all of which the Radix Tabs, Dialog and Select on this page
// reach for.
// ---------------------------------------------------------------------------
if (typeof global.ResizeObserver === "undefined") {
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// ---------------------------------------------------------------------------
// Module mocks (hoisted by Vitest before imports)
// ---------------------------------------------------------------------------

const { mockApiFetch } = vi.hoisted(() => ({ mockApiFetch: vi.fn() }));

vi.mock("@/lib/api/client", () => ({
  apiFetch: mockApiFetch,
  ApiError: class ApiError extends Error {
    constructor(
      public payload: { error_code: string; message: string },
      public status: number,
    ) {
      super(payload.message);
      this.name = "ApiError";
    }
  },
}));

const mockUseMeFn = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({ useMe: () => mockUseMeFn() }));

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

import ProfileTokensPage from "./page";

// ---------------------------------------------------------------------------
// Fixtures — inline, mirroring the two ASCII tables in FRONTEND_BASIC.md
// §API tokens. Stamps are pushed far into the past / future so each row's
// status is decided by the fixture and not by when the suite happens to run.
// ---------------------------------------------------------------------------

const LONG_PAST = "2020-02-01T00:00:00Z";
const LONG_FUTURE = "2099-07-01T00:00:00Z";

const ADMIN_ID = "u-admin";

function makeMe(role: UserRole, overrides: Partial<Me> = {}): Me {
  return {
    id: ADMIN_ID,
    email: "admin@imazon.com",
    name: "Admin",
    role,
    has_password: true,
    has_google: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/** `GET /auth/api-tokens` — the caller's own tokens, active only. */
const MY_TOKENS: { offset: number; limit: number; total_count: number; tokens: ApiTokenItem[] } = {
  offset: 0,
  limit: 20,
  total_count: 1,
  tokens: [
    {
      id: "t-own",
      name: "my-laptop-cli",
      role_snapshot: "Admin",
      created_at: "2026-05-10T00:00:00Z",
      last_used_at: null,
      expires_at: null,
    },
  ],
};

/**
 * `GET /admin/api-tokens` — the deployment-wide inventory. One row per status
 * so a Status-column bug cannot hide behind a single-state fixture, plus the
 * caller's own row, which the spec routes through the admin endpoint like any
 * other.
 */
const ALL_TOKENS_ROWS: AdminApiTokenItem[] = [
  {
    id: "t-alice",
    name: "ci-jenkins",
    role_snapshot: "Editor",
    created_at: "2026-04-01T00:00:00Z",
    last_used_at: "2026-05-25T00:00:00Z",
    expires_at: LONG_FUTURE,
    revoked_at: null,
    user_id: "u-alice",
    user_email: "alice@imazon.com",
  },
  {
    id: "t-bob",
    name: "laptop-cli",
    role_snapshot: "Reader",
    created_at: "2026-05-10T00:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: "2026-05-20T00:00:00Z",
    user_id: "u-bob",
    user_email: "bob@imazon.com",
  },
  {
    id: "t-carol",
    name: "etl-runner",
    role_snapshot: "Reader",
    created_at: "2025-11-02T00:00:00Z",
    last_used_at: "2026-01-08T00:00:00Z",
    expires_at: LONG_PAST,
    revoked_at: null,
    user_id: "u-carol",
    user_email: "carol@imazon.com",
  },
  {
    id: "t-admin-own",
    name: "admin-own-token",
    role_snapshot: "Admin",
    created_at: "2026-06-01T00:00:00Z",
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
    user_id: ADMIN_ID,
    user_email: "admin@imazon.com",
  },
];

/**
 * `total_count` deliberately exceeds the page — the inventory is unbounded, and
 * the paging assertions below need a second page to move to.
 */
const ALL_TOKENS_TOTAL = 45;

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

interface FetchInit {
  method?: string;
}

/** Every request the page issued, in order, as `[path, init]` pairs. */
function calls(): Array<[string, FetchInit | undefined]> {
  return mockApiFetch.mock.calls.map(
    (c) => [String(c[0]), c[1] as FetchInit | undefined] as [string, FetchInit | undefined],
  );
}

function callsTo(prefix: string): Array<[string, FetchInit | undefined]> {
  return calls().filter(([path]) => path.startsWith(prefix));
}

function makeQueryClient(gcTime = 0): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime },
      mutations: { retry: false },
    },
  });
}

function renderPage(qc: QueryClient = makeQueryClient()) {
  const utils = render(
    <QueryClientProvider client={qc}>
      <ProfileTokensPage />
    </QueryClientProvider>,
  );
  return { qc, ...utils };
}

/** Select the All-tokens scope. Radix activates a tab on mousedown/focus. */
async function selectAllTokensScope(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("tab", { name: "All tokens" }));
}

/** The table row carrying `text`, as a scope for per-row assertions. */
function rowFor(text: string): HTMLElement {
  const cell = screen.getByRole("cell", { name: text });
  const row = cell.closest("tr");
  if (!row) throw new Error(`No table row found for "${text}"`);
  return row as HTMLElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseMeFn.mockReturnValue({
    me: makeMe("Admin"),
    isAdmin: true,
    isEditor: false,
    canWrite: true,
    isLoading: false,
  });
  mockApiFetch.mockImplementation(async (path: string, init?: FetchInit) => {
    if (path.startsWith("/auth/api-tokens")) {
      if (init?.method === "DELETE") return undefined;
      if (init?.method === "POST") return { token: "dsk_new_token", id: "t-new" };
      return MY_TOKENS;
    }
    if (path.startsWith("/admin/api-tokens")) {
      return { offset: 0, limit: 20, total_count: ALL_TOKENS_TOTAL, tokens: ALL_TOKENS_ROWS };
    }
    if (/^\/admin\/users\/[^/]+\/api-tokens\/[^/]+$/.test(path)) return undefined;
    throw new Error(`Unexpected apiFetch call: ${init?.method ?? "GET"} ${path}`);
  });
});

// ===========================================================================
// 1. Scope control visibility — Admin only
// ===========================================================================

describe("/profile/tokens — the scope control renders for Admins only", () => {
  it("gives an Admin a My tokens / All tokens control, defaulting to My tokens", async () => {
    renderPage();

    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["My tokens", "All tokens"]);
    expect(screen.getByRole("tab", { name: "My tokens" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "All tokens" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    // The default scope shows the caller's own tokens, unchanged from before
    // the control existed.
    expect(await screen.findByRole("cell", { name: "my-laptop-cli" })).toBeTruthy();
  });

  for (const role of ["Reader", "Editor"] as const) {
    it(`gives a ${role} no scope control at all`, async () => {
      mockUseMeFn.mockReturnValue({
        me: makeMe(role, { id: "u-other", email: `${role.toLowerCase()}@imazon.com` }),
        isAdmin: false,
        isEditor: role === "Editor",
        canWrite: role === "Editor",
        isLoading: false,
      });
      renderPage();

      // Backstop: the page rendered its own table, so the absence below is a
      // real absence and not an unrendered component.
      expect(await screen.findByRole("cell", { name: "my-laptop-cli" })).toBeTruthy();
      expect(screen.queryAllByRole("tab")).toHaveLength(0);
      expect(screen.queryAllByRole("tablist")).toHaveLength(0);
      expect(screen.queryByText("All tokens")).toBeNull();
    });
  }

  it("leaves a non-admin's tree with no tabpanel to label", async () => {
    // The page branches on the whole header + table rather than only hiding the
    // triggers, so a Reader gets no orphan tabpanel wrapping their own table.
    mockUseMeFn.mockReturnValue({
      me: makeMe("Reader", { id: "u-other", email: "reader@imazon.com" }),
      isAdmin: false,
      isEditor: false,
      canWrite: false,
      isLoading: false,
    });
    renderPage();

    expect(await screen.findByRole("cell", { name: "my-laptop-cli" })).toBeTruthy();
    expect(screen.queryAllByRole("tabpanel")).toHaveLength(0);
  });
});

// ===========================================================================
// 2. The admin request itself
// ===========================================================================

describe("/profile/tokens — GET /admin/api-tokens is issued only where the spec allows", () => {
  for (const role of ["Reader", "Editor"] as const) {
    it(`never fires for a ${role}, whatever the DOM shows`, async () => {
      mockUseMeFn.mockReturnValue({
        me: makeMe(role, { id: "u-other", email: `${role.toLowerCase()}@imazon.com` }),
        isAdmin: false,
        isEditor: role === "Editor",
        canWrite: role === "Editor",
        isLoading: false,
      });
      renderPage();

      await screen.findByRole("cell", { name: "my-laptop-cli" });
      // Backstop for the absence: the page did reach the network, and the
      // Admin case below proves this same harness does record the admin call
      // when it is made.
      expect(callsTo("/auth/api-tokens").length).toBeGreaterThan(0);
      expect(callsTo("/admin/")).toEqual([]);
    });
  }

  it("does not fire for an Admin sitting in the default My-tokens scope", async () => {
    renderPage();

    await screen.findByRole("cell", { name: "my-laptop-cli" });
    expect(callsTo("/admin/api-tokens")).toEqual([]);
  });

  it("fires with the route's default paging and revoked rows excluded once All tokens is selected", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });

    await selectAllTokensScope(user);

    await waitFor(() => expect(callsTo("/admin/api-tokens").length).toBeGreaterThan(0));
    const [path] = callsTo("/admin/api-tokens")[0]!;
    expect(path).toMatch(/^\/admin\/api-tokens\?/);
    const params = new URLSearchParams(path.split("?")[1]);
    expect(params.get("offset")).toBe("0");
    expect(params.get("limit")).toBe("20");
    // The spec fixes the default state of the checkbox, not whether the param
    // rides along when it is off — the route's own default is `false`, so
    // omitting it is equally conformant. What must never happen is asking for
    // revoked rows before anyone ticked the box.
    expect(params.get("include_revoked")).not.toBe("true");
  });
});

// ===========================================================================
// 3. The All-tokens table
// ===========================================================================

describe("/profile/tokens — the All-tokens table", () => {
  it("adds Owner and Status to the columns the My-tokens table already had", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });

    // The My-tokens table has neither column — the contrast is the point.
    expect(screen.queryByRole("columnheader", { name: "Owner" })).toBeNull();
    expect(screen.queryByRole("columnheader", { name: "Status" })).toBeNull();

    await selectAllTokensScope(user);

    expect(await screen.findByRole("columnheader", { name: "Owner" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Status" })).toBeTruthy();
    for (const header of ["Name", "Role", "Created", "Last used", "Expires"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeTruthy();
    }
  });

  it("names each row's owner and classifies its status three ways", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });
    await selectAllTokensScope(user);

    await screen.findByRole("cell", { name: "ci-jenkins" });

    // active — unrevoked, expiry ahead
    const alice = rowFor("ci-jenkins");
    expect(within(alice).getByRole("cell", { name: "alice@imazon.com" })).toBeTruthy();
    expect(within(alice).getByText("active")).toBeTruthy();

    // revoked — withdrawn, and it never expires, so expiry cannot be what
    // produced the label
    const bob = rowFor("laptop-cli");
    expect(within(bob).getByRole("cell", { name: "bob@imazon.com" })).toBeTruthy();
    expect(within(bob).getByText("revoked")).toBeTruthy();

    // expired — unrevoked but past its stamp; the route does not filter expiry,
    // so this row is in the default page
    const carol = rowFor("etl-runner");
    expect(within(carol).getByRole("cell", { name: "carol@imazon.com" })).toBeTruthy();
    expect(within(carol).getByText("expired")).toBeTruthy();
  });

  it("pages the inventory against the envelope's total_count", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });
    await selectAllTokensScope(user);
    await screen.findByRole("cell", { name: "ci-jenkins" });

    // The My-tokens scope carries no pagination — it is capped at 10 active
    // tokens per user — so finding the control here is meaningful.
    expect(screen.getByText(`1–20 of ${ALL_TOKENS_TOTAL}`)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => {
      const last = callsTo("/admin/api-tokens").at(-1)![0];
      expect(new URLSearchParams(last.split("?")[1]).get("offset")).toBe("20");
    });
  });

  it("hides 'New token' in the All-tokens scope and keeps it in My tokens", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });

    expect(screen.getByRole("button", { name: /New token/ })).toBeTruthy();

    await selectAllTokensScope(user);
    await screen.findByRole("cell", { name: "ci-jenkins" });
    // Minting is self-only; the control must not sit above a table of other
    // users' tokens.
    expect(screen.queryByRole("button", { name: /New token/ })).toBeNull();

    await user.click(screen.getByRole("tab", { name: "My tokens" }));
    expect(await screen.findByRole("button", { name: /New token/ })).toBeTruthy();
  });

  it("keeps 'New token' for a non-admin, whose page is unchanged", async () => {
    mockUseMeFn.mockReturnValue({
      me: makeMe("Reader", { id: "u-other", email: "reader@imazon.com" }),
      isAdmin: false,
      isEditor: false,
      canWrite: false,
      isLoading: false,
    });
    renderPage();

    expect(await screen.findByRole("button", { name: /New token/ })).toBeTruthy();
  });
});

// ===========================================================================
// 4. Show revoked
// ===========================================================================

describe("/profile/tokens — the Show revoked toggle", () => {
  it("starts off, so the default view carries the route's default filter", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });
    await selectAllTokensScope(user);

    const toggle = await screen.findByLabelText("Show revoked");
    expect(toggle).toHaveAttribute("data-state", "unchecked");
  });

  it("flips include_revoked to true and resets paging to the first page", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });
    await selectAllTokensScope(user);
    await screen.findByRole("cell", { name: "ci-jenkins" });

    // Move off page 1 first — otherwise "resets to offset=0" is satisfied by
    // never having left it.
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => {
      const last = callsTo("/admin/api-tokens").at(-1)![0];
      expect(new URLSearchParams(last.split("?")[1]).get("offset")).toBe("20");
    });

    await user.click(screen.getByLabelText("Show revoked"));

    await waitFor(() => {
      const last = callsTo("/admin/api-tokens").at(-1)![0];
      const params = new URLSearchParams(last.split("?")[1]);
      expect(params.get("include_revoked")).toBe("true");
      // The result set changed, so the page number no longer means anything.
      expect(params.get("offset")).toBe("0");
    });
  });

  it("is offered only in the All-tokens scope", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });

    expect(screen.queryByLabelText("Show revoked")).toBeNull();

    await selectAllTokensScope(user);
    expect(await screen.findByLabelText("Show revoked")).toBeTruthy();

    await user.click(screen.getByRole("tab", { name: "My tokens" }));
    await waitFor(() => expect(screen.queryByLabelText("Show revoked")).toBeNull());
  });
});

// ===========================================================================
// 5. Revoke affordance and routing
// ===========================================================================

describe("/profile/tokens — the revoke affordance in the All-tokens scope", () => {
  async function openAllTokens() {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });
    await selectAllTokensScope(user);
    await screen.findByRole("cell", { name: "ci-jenkins" });
    return user;
  }

  it("offers no Revoke on a revoked row and does offer one on an expired row", async () => {
    await openAllTokens();

    // A revoked token grants nothing, so there is nothing left to withdraw.
    expect(within(rowFor("laptop-cli")).queryByRole("button", { name: "Revoke" })).toBeNull();
    // Expiry is a clock, revocation is a decision — an expired token is still
    // revocable.
    expect(within(rowFor("etl-runner")).getByRole("button", { name: "Revoke" })).toBeTruthy();
    // …as is an active one, so the revoked row is the only one missing it.
    expect(within(rowFor("ci-jenkins")).getByRole("button", { name: "Revoke" })).toBeTruthy();
  });

  it("revokes another user's token through the admin route, addressed by the row's user_id", async () => {
    const user = await openAllTokens();

    await user.click(within(rowFor("etl-runner")).getByRole("button", { name: "Revoke" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/etl-runner/)).toBeTruthy();
    expect(within(dialog).getByText(/carol@imazon\.com/)).toBeTruthy();
    // Opening the confirm must not act.
    expect(callsTo("/admin/users/")).toEqual([]);

    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));

    await waitFor(() =>
      expect(callsTo("/admin/users/")).toEqual([
        ["/admin/users/u-carol/api-tokens/t-carol", { method: "DELETE" }],
      ]),
    );
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(expect.objectContaining({ title: "Token revoked." })),
    );
  });

  it("revokes the Admin's own token through the same admin route, not the self route", async () => {
    const user = await openAllTokens();

    await user.click(within(rowFor("admin-own-token")).getByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));

    // One route, one confirm copy — the caller's own row is addressed by its
    // user_id like every other.
    await waitFor(() =>
      expect(callsTo("/admin/users/")).toEqual([
        [`/admin/users/${ADMIN_ID}/api-tokens/t-admin-own`, { method: "DELETE" }],
      ]),
    );
    expect(calls().filter(([p, i]) => p.startsWith("/auth/api-tokens/") && i?.method === "DELETE"))
      .toEqual([]);
  });
});

describe("/profile/tokens — the revoke affordance in the My-tokens scope", () => {
  it("revokes through the self route", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("cell", { name: "my-laptop-cli" });

    await user.click(within(rowFor("my-laptop-cli")).getByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));

    await waitFor(() =>
      expect(
        calls().filter(([p, i]) => p.startsWith("/auth/api-tokens/") && i?.method === "DELETE"),
      ).toEqual([["/auth/api-tokens/t-own", { method: "DELETE" }]]),
    );
    // The self route owns this scope; the admin route is not consulted.
    expect(callsTo("/admin/users/")).toEqual([]);
  });
});

// ===========================================================================
// 6. Caller-scoped cache
//
// NOT-A-SPEC-CLAIM: no sentence in FRONTEND_BASIC.md, AUTH.md or API.md governs
// client-side cache keying — the five traces in this file's header do not cover
// this section. What the two tests below pin is a cross-session invariant of the
// client: one browser tab's QueryClient must never paint one Admin's
// deployment-wide inventory to the next person signed in at that tab. The
// caller-id-in-key is, in `lib/api/admin.ts`'s own words, "defence in depth, not
// the fix for a cross-session paint" — so these assert the observable leak, not
// the mechanism, and they will still hold if the mechanism is replaced by a
// cache reset on sign-out. Read them as a regression guard on the leak, not as
// evidence of a spec requirement.
// ===========================================================================

describe("/profile/tokens — the inventory cache is scoped to the signed-in Admin", () => {
  /**
   * A QueryClient outlives a sign-out inside one tab. These two tests share a
   * client across an unmount/remount and hold the second read open, so what is
   * asserted is what the second session SEES while its own request is in
   * flight — which is exactly the window a shared cache key would paint the
   * previous Admin's inventory in.
   */
  async function primeInventory(qc: QueryClient) {
    const user = userEvent.setup();
    const { unmount } = renderPage(qc);
    await screen.findByRole("cell", { name: "my-laptop-cli" });
    await selectAllTokensScope(user);
    await screen.findByRole("cell", { name: "alice@imazon.com" });
    unmount();
  }

  /** Hold every subsequent inventory read open, so only the cache can paint. */
  function suspendInventoryReads() {
    const previous = mockApiFetch.getMockImplementation()!;
    mockApiFetch.mockImplementation(async (path: string, init?: FetchInit) => {
      if (path.startsWith("/admin/api-tokens")) return new Promise(() => {});
      return previous(path, init);
    });
  }

  it("repaints the cached inventory for the same Admin while it revalidates", async () => {
    const qc = makeQueryClient(Infinity);
    await primeInventory(qc);
    suspendInventoryReads();

    const user = userEvent.setup();
    renderPage(qc);
    await screen.findByRole("cell", { name: "my-laptop-cli" });
    await selectAllTokensScope(user);

    // Positive leg: the harness CAN paint from cache with the read suspended.
    expect(await screen.findByRole("cell", { name: "alice@imazon.com" })).toBeTruthy();
  });

  it("shows a different Admin nothing from the previous session's cache", async () => {
    const qc = makeQueryClient(Infinity);
    await primeInventory(qc);
    suspendInventoryReads();

    // A second Admin signs in in the same tab; the QueryClient is the same.
    mockUseMeFn.mockReturnValue({
      me: makeMe("Admin", { id: "u-second-admin", email: "second-admin@imazon.com" }),
      isAdmin: true,
      isEditor: false,
      canWrite: true,
      isLoading: false,
    });

    const user = userEvent.setup();
    renderPage(qc);
    await screen.findByRole("cell", { name: "my-laptop-cli" });
    const before = callsTo("/admin/api-tokens").length;
    await selectAllTokensScope(user);

    // The second session issues its own read…
    await waitFor(() =>
      expect(callsTo("/admin/api-tokens").length).toBeGreaterThan(before),
    );
    // …and, while that read is in flight, paints none of the first session's rows.
    expect(screen.queryByRole("cell", { name: "alice@imazon.com" })).toBeNull();
    expect(screen.queryByRole("cell", { name: "ci-jenkins" })).toBeNull();
  });
});

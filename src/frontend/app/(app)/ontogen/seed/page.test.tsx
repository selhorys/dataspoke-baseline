/**
 * Tests for app/(app)/ontogen/seed/page.tsx — OntoGen Seed Library page.
 *
 * Behavior under test (UC3 seed enabled/disabled lifecycle):
 *   - The library lists ALL seeds — enabled AND disabled — each row showing an
 *     enabled/disabled badge and a per-seed enable/disable toggle.
 *   - The toggle calls useSetSeedEnabled(seedId).mutate with the NEGATED flag
 *     (an enabled seed's button disables it; a disabled seed's button enables it).
 *   - `+ New Seed` creates a seed via the create hook; new seeds ship disabled
 *     (the create call carries only the Markdown body — disabled-by-default is the
 *     backend Factory-default, and the page copy advertises it).
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_ONTOGEN.md §Seed library — the library lists all seeds
 *     (enabled and disabled), each row with an enabled/disabled indicator and a
 *     per-seed enable/disable toggle (PATCH .../attr/seed/{seed_id}/attr/enabled);
 *     `+ New Seed` (POST .../attr/seed) creates the seed disabled.
 *   - spec/USE_CASE_en.md §UC3 — a disabled seed stays visible and re-enableable;
 *     enabling/disabling is reversible.
 *   - lib/api/ontogen.ts useSetSeedEnabled → PATCH .../attr/seed/{id}/attr/enabled
 *     {is_enabled}; useCreateSeed → POST .../attr/seed (Markdown body).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SeedListItem } from "@/types/ontogen";

// ---------------------------------------------------------------------------
// Module mocks (hoisted by Vitest before imports)
// ---------------------------------------------------------------------------

// useMe — canWrite:true so the per-seed action controls render
const mockUseMeFn = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({
  useMe: () => mockUseMeFn(),
}));

// ontogen API hooks — controllable per-test. setSeedEnabled mutate is captured
// per seedId so we can assert the toggle sends the negated flag for the right seed.
const mockUseOntogenSeeds = vi.fn();
const setEnabledMutateBySeed = new Map<string, ReturnType<typeof vi.fn>>();
const mockCreateMutate = vi.fn();

function setEnabledMutateFor(seedId: string): ReturnType<typeof vi.fn> {
  let fn = setEnabledMutateBySeed.get(seedId);
  if (!fn) {
    fn = vi.fn();
    setEnabledMutateBySeed.set(seedId, fn);
  }
  return fn;
}

vi.mock("@/lib/api/ontogen", () => ({
  useOntogenSeeds: () => mockUseOntogenSeeds(),
  // The open-seed body fetch — gated to "" (closed) in these tests, returns nothing.
  useOntogenSeed: () => ({ data: undefined, isLoading: false }),
  useCreateSeed: () => ({ mutate: mockCreateMutate, isPending: false }),
  useUpdateSeed: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteSeed: () => ({ mutate: vi.fn(), isPending: false }),
  useSetSeedEnabled: (seedId: string) => ({
    mutate: setEnabledMutateFor(seedId),
    isPending: false,
  }),
}));

// toast — capture calls (not asserted, just stubbed)
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// timezone preference — stubbed so formatDateTime renders deterministically
vi.mock("@/lib/preferences/timezone", () => ({
  useDisplayTz: () => "UTC",
}));

// ---------------------------------------------------------------------------
// Import the page AFTER mocks are registered
// ---------------------------------------------------------------------------
import OntogenSeedPage from "./page";

// ---------------------------------------------------------------------------
// Helpers / fixtures
// ---------------------------------------------------------------------------
function editorMe() {
  return {
    me: {
      id: "u1",
      email: "editor@example.com",
      name: "Editor",
      role: "Editor" as const,
      has_password: true,
      has_google: false,
      created_at: "",
      updated_at: "",
    },
    isAdmin: false,
    isEditor: true,
    canWrite: true,
    isLoading: false,
  };
}

function makeSeed(overrides: Partial<SeedListItem> = {}): SeedListItem {
  return {
    seed_id: "seed-1",
    is_enabled: false,
    updated_at: "2026-05-08T00:00:00Z",
    preview: "# Domain seed",
    ...overrides,
  };
}

/** The <li> row for a given seed, scoped by its monospace seed_id label. */
function seedRow(seedId: string): HTMLElement {
  const label = screen.getByText(seedId);
  const li = label.closest("li");
  if (!li) throw new Error(`seed row <li> for ${seedId} not found`);
  return li as HTMLElement;
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
beforeEach(() => {
  vi.clearAllMocks();
  setEnabledMutateBySeed.clear();
  cleanup();
  mockUseMeFn.mockReturnValue(editorMe());
});

// ---------------------------------------------------------------------------
// 1. The library shows BOTH enabled and disabled seeds with their indicator
// ---------------------------------------------------------------------------
describe("OntogenSeedPage — lists all seeds with enabled/disabled indicator (FRONTEND_ONTOGEN.md §Seed library)", () => {
  it("renders a disabled badge for is_enabled=false and an enabled badge for is_enabled=true", () => {
    mockUseOntogenSeeds.mockReturnValue({
      data: {
        seeds: [
          makeSeed({ seed_id: "seed-off", is_enabled: false }),
          makeSeed({ seed_id: "seed-on", is_enabled: true }),
        ],
      },
      isLoading: false,
    });

    render(<OntogenSeedPage />);

    // Both rows are present — a disabled seed is NOT hidden from the library.
    const offRow = seedRow("seed-off");
    const onRow = seedRow("seed-on");

    expect(within(offRow).getByText("disabled")).toBeInTheDocument();
    expect(within(onRow).getByText("enabled")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 2. The per-seed toggle calls useSetSeedEnabled with the NEGATED flag
// ---------------------------------------------------------------------------
describe("OntogenSeedPage — per-seed enable/disable toggle (PATCH .../attr/enabled)", () => {
  it("a disabled seed's toggle (labelled Enable) calls mutate(true)", async () => {
    const user = userEvent.setup();
    mockUseOntogenSeeds.mockReturnValue({
      data: { seeds: [makeSeed({ seed_id: "seed-off", is_enabled: false })] },
      isLoading: false,
    });

    render(<OntogenSeedPage />);

    const row = seedRow("seed-off");
    // A disabled seed offers an "Enable" affordance.
    const toggle = within(row).getByRole("button", { name: /^enable$/i });
    await user.click(toggle);

    await waitFor(() => {
      expect(setEnabledMutateFor("seed-off")).toHaveBeenCalledTimes(1);
    });
    // Negated flag: a disabled seed enables → mutate(true).
    expect(setEnabledMutateFor("seed-off").mock.calls[0][0]).toBe(true);
  });

  it("an enabled seed's toggle (labelled Disable) calls mutate(false)", async () => {
    const user = userEvent.setup();
    mockUseOntogenSeeds.mockReturnValue({
      data: { seeds: [makeSeed({ seed_id: "seed-on", is_enabled: true })] },
      isLoading: false,
    });

    render(<OntogenSeedPage />);

    const row = seedRow("seed-on");
    const toggle = within(row).getByRole("button", { name: /^disable$/i });
    await user.click(toggle);

    await waitFor(() => {
      expect(setEnabledMutateFor("seed-on")).toHaveBeenCalledTimes(1);
    });
    // Negated flag: an enabled seed disables → mutate(false).
    expect(setEnabledMutateFor("seed-on").mock.calls[0][0]).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 3. + New Seed creates a seed (disabled by default)
// ---------------------------------------------------------------------------
describe("OntogenSeedPage — + New Seed creates a disabled seed (POST .../attr/seed)", () => {
  it("opening + New Seed and saving fires the create mutation with only the Markdown body", async () => {
    const user = userEvent.setup();
    mockUseOntogenSeeds.mockReturnValue({
      data: { seeds: [] },
      isLoading: false,
    });

    render(<OntogenSeedPage />);

    // Open the new-seed editor.
    await user.click(screen.getByRole("button", { name: /\+ new seed/i }));

    // The SeedEditor exposes a Markdown textarea and a Save control.
    const textarea = await screen.findByRole("textbox");
    await user.type(textarea, "# A fresh seed");

    await user.click(screen.getByRole("button", { name: /^save seed$/i }));

    await waitFor(() => {
      expect(mockCreateMutate).toHaveBeenCalledTimes(1);
    });
    // The create call carries the Markdown body only — disabled-by-default is the
    // backend Factory-default, so the client sends no is_enabled flag on create.
    const arg = mockCreateMutate.mock.calls[0][0];
    expect(typeof arg).toBe("string");
    expect(arg).toContain("# A fresh seed");
  });

  it("the page surfaces that a newly created seed starts disabled", () => {
    mockUseOntogenSeeds.mockReturnValue({ data: { seeds: [] }, isLoading: false });
    render(<OntogenSeedPage />);
    // spec: FRONTEND_ONTOGEN.md §Page contracts (Seed library) — "`+ New Seed` (`POST .../attr/seed`)
    // creates the seed **disabled** — the steward enables it once the body is
    // reviewed." The spec mandates the behaviour, not a wording, so match on the
    // create-disabled fact rather than on the current sentence verbatim.
    expect(screen.getByText(/new seed.*disabled/i)).toBeInTheDocument();
  });
});

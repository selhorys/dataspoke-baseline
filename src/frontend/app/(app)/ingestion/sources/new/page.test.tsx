/**
 * Tests for the create-ingestion-source page.
 *
 * The page owns mode/name/schedule; the recipe editor validates only the recipe
 * shape (recipeOnly) and hands the parsed recipe back. The page composes the
 * full body, runs the complete validateSourceBody, and POSTs.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Create View.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import CreateIngestionSourcePage from "./page";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({ useMe: () => mockUseMe() }));

const mutate = vi.fn();
const mockCreate = vi.fn();
const mockSecrets = vi.fn();
vi.mock("@/lib/api/ingestion", () => ({
  useCreateIngestionSource: () => mockCreate(),
  useIngestionSecrets: () => mockSecrets(),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

beforeEach(() => {
  push.mockReset();
  mutate.mockReset();
  mockUseMe.mockReturnValue({ canWrite: true });
  mockCreate.mockReturnValue({ mutate, isPending: false, error: null });
  mockSecrets.mockReturnValue({ data: { secrets: [] }, error: null });
});

describe("CreateIngestionSourcePage", () => {
  it("blocks Readers from the create form", () => {
    mockUseMe.mockReturnValue({ canWrite: false });
    render(<CreateIngestionSourcePage />);
    expect(
      screen.getByText(/need the Editor role to create an ingestion source/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
  });

  it("composes name + schedule from page state and POSTs the full body", () => {
    render(<CreateIngestionSourcePage />);

    // The default template has no `name`/`schedule` — the page owns those. Drive
    // the create end-to-end: type a name, then Save.
    fireEvent.change(screen.getByLabelText(/name/i), {
      target: { value: "prod postgres" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    expect(mutate).toHaveBeenCalledTimes(1);
    const body = mutate.mock.calls[0][0];
    expect(body).toMatchObject({
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: "prod postgres",
      schedule: "0 0 * * *",
    });
    // Recipe came from the template, parsed losslessly.
    expect(body.recipe).toMatchObject({ source: { type: "postgres" } });
  });

  it("surfaces the missing-name validation error inline instead of swallowing it", () => {
    render(<CreateIngestionSourcePage />);
    // No name typed → composed-body validation fails before POST.
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(mutate).not.toHaveBeenCalled();
    expect(screen.getByText(/name must be a string of 1–512 characters/i)).toBeInTheDocument();
  });
});

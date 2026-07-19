/**
 * Tests for components/datahub-dataset-link.tsx — the shared DataHub deep-link.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Per-dataset page / §Shell: a shared DataHub
 *     dataset deep-link affordance (`<datahubUrl>/dataset/{urn}`) reused across
 *     dataset tables and the per-dataset header; rendered only when `datahubUrl`
 *     resolves non-empty (mirrors the app-shell infra-link gating).
 *   - spec/feature/FRONTEND_BASIC.md §Shell: the DataHub URL resolves env-first,
 *     then GET /spoke/common/peripheral-links — here supplied via useDisplayLinks.
 *   - The URL is `${datahubUrl}/dataset/${encodeURIComponent(urn)}` — URNs carry
 *     `: ( ) ,` which must be percent-encoded.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { DatahubDatasetLink, datahubDatasetUrl } from "./datahub-dataset-link";

// The component resolves its base URL through useDisplayLinks (env-first, then
// the peripheral-links endpoint); control the resolved value per test.
const mockUseDisplayLinks = vi.fn();
vi.mock("@/lib/api/peripheral-links", () => ({
  useDisplayLinks: () => mockUseDisplayLinks(),
}));

const URN =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";

beforeEach(() => {
  mockUseDisplayLinks.mockReset();
});

// ── datahubDatasetUrl helper ────────────────────────────────────────────────────

describe("datahubDatasetUrl", () => {
  it("builds ${datahubUrl}/dataset/${encodeURIComponent(urn)} when configured", () => {
    expect(datahubDatasetUrl("http://datahub.example.com", URN)).toBe(
      `http://datahub.example.com/dataset/${encodeURIComponent(URN)}`,
    );
  });

  it("returns null when no datahubUrl is configured", () => {
    expect(datahubDatasetUrl("", URN)).toBeNull();
  });
});

// ── DatahubDatasetLink rendering ────────────────────────────────────────────────

describe("DatahubDatasetLink — configured", () => {
  beforeEach(() => {
    mockUseDisplayLinks.mockReturnValue({ datahubUrl: "http://datahub.example.com" });
  });

  it("renders an external link with the encoded dataset href", () => {
    render(<DatahubDatasetLink urn={URN} />);
    const link = screen.getByRole("link", { name: /datahub/i });
    expect((link as HTMLAnchorElement).getAttribute("href")).toBe(
      `http://datahub.example.com/dataset/${encodeURIComponent(URN)}`,
    );
    // Opens in a new tab safely.
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  it("uses the default 'DataHub' label and supports a custom label", () => {
    const { rerender } = render(<DatahubDatasetLink urn={URN} />);
    expect(screen.getByText("DataHub")).toBeTruthy();
    rerender(<DatahubDatasetLink urn={URN} label="Open in DataHub" />);
    expect(screen.getByText("Open in DataHub")).toBeTruthy();
  });

  it("percent-encodes URN special characters ( : ( ) , ) in the href", () => {
    render(<DatahubDatasetLink urn={URN} />);
    const href = (screen.getByRole("link") as HTMLAnchorElement).getAttribute("href")!;
    // The raw, unencoded parenthesis/comma must not leak into the path.
    expect(href).not.toContain("(urn:li:dataPlatform");
    expect(href).toContain(encodeURIComponent(URN));
  });
});

describe("DatahubDatasetLink — unconfigured", () => {
  beforeEach(() => {
    mockUseDisplayLinks.mockReturnValue({ datahubUrl: "" });
  });

  it("renders nothing (no link) by default when datahubUrl is unset", () => {
    const { container } = render(<DatahubDatasetLink urn={URN} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("renders the provided fallback when datahubUrl is unset", () => {
    render(<DatahubDatasetLink urn={URN} fallback={<span>—</span>} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("—")).toBeTruthy();
  });
});

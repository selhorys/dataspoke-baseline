/**
 * Tests for SecretRefHelper.
 *
 * The component is read-only: it renders the available `${name__key}` references
 * from GET /spoke/ingestion/secrets plus a static authoring guide (kubectl
 * recipe, dataspoke-source-cred- prefix, ${name__key} syntax). It must never
 * issue a write call — DataSpoke is reference-only.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Create View / §Components.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { SecretRefHelper } from "./secret-ref-helper";
import type { SecretRefInfo } from "@/types/ingestion";

const SECRETS: SecretRefInfo[] = [
  { ref: "dummy-data-pg__password", secret_name: "dataspoke-source-cred-dummy-data-pg", key: "password" },
];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SecretRefHelper", () => {
  it("lists available ${name__key} references from the secrets payload", () => {
    render(<SecretRefHelper secrets={SECRETS} />);
    expect(
      screen.getByText("${dummy-data-pg__password}"),
    ).toBeInTheDocument();
  });

  it("renders the kubectl authoring recipe with the dataspoke-source-cred- prefix", () => {
    render(<SecretRefHelper secrets={SECRETS} />);

    // The kubectl create-secret recipe is rendered verbatim in a <pre>.
    const recipe = screen.getByText(/kubectl create secret generic/);
    expect(recipe.textContent).toContain(
      "dataspoke-source-cred-<name>",
    );
    expect(recipe.textContent).toContain("--from-literal=<key>=<value>");
    expect(recipe.textContent).toContain("-n <dataspoke-namespace>");
  });

  it("documents the prefix as a security boundary and the ${name__key} reference syntax", () => {
    render(<SecretRefHelper secrets={SECRETS} />);

    // Security-boundary prefix appears in the guide bullets.
    expect(
      screen.getByText(/security boundary/i),
    ).toBeInTheDocument();
    // The reference-syntax token is shown.
    const refTokens = screen.getAllByText("${name__key}");
    expect(refTokens.length).toBeGreaterThan(0);
  });

  it("notes the in-cluster namespace for the Secret", () => {
    render(<SecretRefHelper secrets={SECRETS} />);
    expect(screen.getByText(/in-cluster\) namespace/i)).toBeInTheDocument();
  });

  it("shows an empty-state when no references exist, still without a write call", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<SecretRefHelper secrets={[]} />);
    expect(
      screen.getByText(/No source-credential references available/i),
    ).toBeInTheDocument();
    // Read-only component: it never fires any HTTP call (write or otherwise).
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("renders the 503-unavailable hint without crashing", () => {
    render(<SecretRefHelper unavailable />);
    expect(
      screen.getByText(/Secret store is unavailable \(503\)/i),
    ).toBeInTheDocument();
    // Authoring guide is still present so authors can self-serve.
    expect(screen.getByText(/kubectl create secret generic/)).toBeInTheDocument();
  });

  it("issues no fetch/XHR for the static authoring guide", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<SecretRefHelper secrets={SECRETS} />);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

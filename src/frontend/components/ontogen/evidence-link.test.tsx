/**
 * Tests for components/ontogen/evidence-link.tsx — the Langfuse trace deep-link.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_ONTOGEN.md: a result row's `run_id` doubles as its
 *     Langfuse session id; the Evidence cell links to
 *     {langfuseUrl}/project/{projectId}/sessions/{run_id} in a new tab, and
 *     renders an em dash when the run or the Langfuse wiring is absent.
 *   - spec/feature/FRONTEND_BASIC.md §Shell: langfuseUrl / langfuseProjectId
 *     resolve env-first, then GET /spoke/common/peripheral-links — supplied here
 *     through useDisplayLinks.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { EvidenceLink } from "./evidence-link";

const mockUseDisplayLinks = vi.fn();
vi.mock("@/lib/api/peripheral-links", () => ({
  useDisplayLinks: () => mockUseDisplayLinks(),
}));

const RUN_ID = "run-7f3a";

function setLinks(langfuseUrl: string, langfuseProjectId: string): void {
  mockUseDisplayLinks.mockReturnValue({
    datahubUrl: "",
    langfuseUrl,
    langfuseProjectId,
  });
}

beforeEach(() => {
  mockUseDisplayLinks.mockReset();
});

describe("EvidenceLink — configured", () => {
  beforeEach(() => {
    setLinks("https://langfuse.example.com", "proj-1");
  });

  it("links to the run's Langfuse session in a new tab", () => {
    render(<EvidenceLink runId={RUN_ID} />);
    const link = screen.getByRole("link", { name: /link/i });
    expect(link.getAttribute("href")).toBe(
      `https://langfuse.example.com/project/proj-1/sessions/${RUN_ID}`,
    );
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
  });

  // Not an independently-spec'd normalization: FRONTEND_ONTOGEN.md §Evidence
  // specifies the resulting href shape (`<langfuse_url>/project/{id}/sessions/{run}`)
  // and the display-link safety rule admits a slash-terminated base
  // (`https://datahub.example.com/` is an accepted corpus case — see
  // tests/fixtures/safe-url-cases.json, Shape row). Stripping is therefore
  // required to hit the spec'd shape from an admissible configured value.
  it("strips a trailing slash from the Langfuse base URL", () => {
    setLinks("https://langfuse.example.com/", "proj-1");
    render(<EvidenceLink runId={RUN_ID} />);
    expect(screen.getByRole("link").getAttribute("href")).toBe(
      `https://langfuse.example.com/project/proj-1/sessions/${RUN_ID}`,
    );
  });

  it("percent-encodes the project id and run id into their path segments", () => {
    setLinks("https://langfuse.example.com", "proj-1");
    render(<EvidenceLink runId="run/../../escape" />);
    const href = screen.getByRole("link").getAttribute("href")!;
    expect(href).toContain(encodeURIComponent("run/../../escape"));
    expect(href).not.toContain("sessions/run/../..");
  });
});

describe("EvidenceLink — unconfigured", () => {
  it("renders an em dash when the row has no run", () => {
    setLinks("https://langfuse.example.com", "proj-1");
    render(<EvidenceLink runId={null} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("—")).toBeTruthy();
  });

  it("renders an em dash when neither plane supplies a Langfuse URL", () => {
    setLinks("", "proj-1");
    render(<EvidenceLink runId={RUN_ID} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("—")).toBeTruthy();
  });

  it("renders an em dash when the project id is unset", () => {
    setLinks("https://langfuse.example.com", "");
    render(<EvidenceLink runId={RUN_ID} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("—")).toBeTruthy();
  });
});

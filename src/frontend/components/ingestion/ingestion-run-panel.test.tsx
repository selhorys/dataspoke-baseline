/**
 * Tests for IngestionRunPanel — run gating by mode/role and error mapping.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Run:
 *   Run is shown only for ACTIVE_CUSTOM_MANAGED; other modes show an
 *   explanatory disabled state with a `datahub-sync-hourly` Airflow-DAG note
 *   (linked when airflowUrl is configured, plain text otherwise).
 *   INGESTION_RUNNING / INGESTION_RUN_NOT_APPLICABLE map to human-readable messages.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { IngestionRunPanel } from "./ingestion-run-panel";
import { ApiError } from "@/lib/api/client";

// The non-runnable branch reads airflowUrl from getRuntimeConfig to gate the
// datahub-sync-hourly DAG link; control it per test. Default: unset.
const mockGetRuntimeConfig = vi.fn();
vi.mock("@/lib/runtime-config", () => ({
  getRuntimeConfig: () => mockGetRuntimeConfig(),
}));

beforeEach(() => {
  mockGetRuntimeConfig.mockReset();
  mockGetRuntimeConfig.mockReturnValue({ airflowUrl: "" });
});

function makeApiError(code: string, status = 409): ApiError {
  return new ApiError(
    {
      error_code: code,
      message: "raw message",
      trace_id: "00000000-0000-0000-0000-000000000000",
      resp_time: new Date().toISOString(),
    },
    status,
  );
}

describe("IngestionRunPanel — mode gating", () => {
  it("shows an explanatory disabled state for PASSIVE", () => {
    render(
      <IngestionRunPanel
        mode="PASSIVE"
        canWrite
        onRun={vi.fn()}
        isRunning={false}
        error={null}
      />,
    );
    expect(screen.getByText(/not available/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /run/i })).toBeNull();
  });

  it("shows an explanatory disabled state for DATAHUB_MANAGED", () => {
    render(
      <IngestionRunPanel
        mode="DATAHUB_MANAGED"
        canWrite
        onRun={vi.fn()}
        isRunning={false}
        error={null}
      />,
    );
    expect(screen.getByText(/not available/i)).toBeTruthy();
  });

  it("shows a role note for ACTIVE when caller cannot write", () => {
    render(
      <IngestionRunPanel
        mode="ACTIVE_CUSTOM_MANAGED"
        canWrite={false}
        onRun={vi.fn()}
        isRunning={false}
        error={null}
      />,
    );
    expect(screen.getByText(/Editor role/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^run$/i })).toBeNull();
  });

  it("renders a runnable control for ACTIVE + canWrite", () => {
    const onRun = vi.fn();
    render(
      <IngestionRunPanel
        mode="ACTIVE_CUSTOM_MANAGED"
        canWrite
        onRun={onRun}
        isRunning={false}
        error={null}
      />,
    );
    const runBtn = screen.getByRole("button", { name: /^run$/i });
    fireEvent.click(runBtn);
    expect(onRun).toHaveBeenCalledWith(false);
  });

  it("passes dry_run=true when the checkbox is checked", () => {
    const onRun = vi.fn();
    render(
      <IngestionRunPanel
        mode="ACTIVE_CUSTOM_MANAGED"
        canWrite
        onRun={onRun}
        isRunning={false}
        error={null}
      />,
    );
    fireEvent.click(screen.getByLabelText(/dry_run/i));
    fireEvent.click(screen.getByRole("button", { name: /dry run/i }));
    expect(onRun).toHaveBeenCalledWith(true);
  });
});

describe("IngestionRunPanel — datahub-sync note (non-runnable modes)", () => {
  // spec: FRONTEND_INGESTION.md §Source Detail §Run — non-active modes add a
  // datahub-sync-hourly DAG description; the DAG name links to Airflow only when
  // airflowUrl is configured, else it renders as plain (font-mono) text.
  it.each(["DATAHUB_MANAGED", "PASSIVE"] as const)(
    "names the datahub-sync-hourly DAG for %s mode",
    (mode) => {
      render(
        <IngestionRunPanel
          mode={mode}
          canWrite
          onRun={vi.fn()}
          isRunning={false}
          error={null}
        />,
      );
      expect(screen.getByText("datahub-sync-hourly")).toBeTruthy();
      // Anchor on the DAG's role descriptor (an Airflow DAG performs the sync),
      // not incidental phrasing.
      expect(screen.getByText(/Airflow DAG/i)).toBeTruthy();
    },
  );

  it("links datahub-sync-hourly to Airflow when airflowUrl is configured", () => {
    mockGetRuntimeConfig.mockReturnValue({ airflowUrl: "http://airflow.example.com" });
    render(
      <IngestionRunPanel
        mode="DATAHUB_MANAGED"
        canWrite
        onRun={vi.fn()}
        isRunning={false}
        error={null}
      />,
    );
    const link = screen.getByRole("link", { name: /datahub-sync-hourly/i });
    expect((link as HTMLAnchorElement).getAttribute("href")).toBe(
      "http://airflow.example.com/dags/datahub-sync-hourly",
    );
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("renders the DAG name as plain text (no link) when airflowUrl is unset", () => {
    // mockGetRuntimeConfig default returns airflowUrl: "".
    render(
      <IngestionRunPanel
        mode="PASSIVE"
        canWrite
        onRun={vi.fn()}
        isRunning={false}
        error={null}
      />,
    );
    expect(screen.getByText("datahub-sync-hourly")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /datahub-sync-hourly/i })).toBeNull();
  });
});

describe("IngestionRunPanel — error mapping", () => {
  it("maps INGESTION_RUNNING to a friendly message", () => {
    render(
      <IngestionRunPanel
        mode="ACTIVE_CUSTOM_MANAGED"
        canWrite
        onRun={vi.fn()}
        isRunning={false}
        error={makeApiError("INGESTION_RUNNING")}
      />,
    );
    expect(screen.getByText(/already in progress/i)).toBeTruthy();
  });

  it("maps INGESTION_RUN_NOT_APPLICABLE to the mode explanation", () => {
    render(
      <IngestionRunPanel
        mode="ACTIVE_CUSTOM_MANAGED"
        canWrite
        onRun={vi.fn()}
        isRunning={false}
        error={makeApiError("INGESTION_RUN_NOT_APPLICABLE")}
      />,
    );
    // modeDescription(ACTIVE_CUSTOM_MANAGED) mentions the custom extractor.
    expect(screen.getByText(/extractor/i)).toBeTruthy();
  });

  it("renders the discovered/emitted summary and the discovered URN list for a real run", () => {
    render(
      <IngestionRunPanel
        mode="ACTIVE_CUSTOM_MANAGED"
        canWrite
        onRun={vi.fn()}
        isRunning={false}
        error={null}
        lastRun={{
          run_id: "run-123",
          status: "success",
          detail: {
            dry_run: false,
            discovered_urns: ["urn:li:dataset:a", "urn:li:dataset:b"],
            discovered_urns_count: 2,
            emitted_urns: ["urn:li:dataset:a", "urn:li:dataset:b"],
            emitted_urns_count: 2,
          },
        }}
      />,
    );
    expect(screen.getByText(/run-123/)).toBeTruthy();
    // Summary line shows both discovered and emitted counts on a real run.
    expect(screen.getByText(/Discovered 2/)).toBeTruthy();
    expect(screen.getByText(/Emitted 2/)).toBeTruthy();
    // The would-emit plan lists every discovered URN.
    expect(screen.getByText("urn:li:dataset:a")).toBeTruthy();
    expect(screen.getByText("urn:li:dataset:b")).toBeTruthy();
  });

  it("shows a no-emit message for a dry run", () => {
    render(
      <IngestionRunPanel
        mode="ACTIVE_CUSTOM_MANAGED"
        canWrite
        onRun={vi.fn()}
        isRunning={false}
        error={null}
        lastRun={{
          run_id: "run-dry",
          status: "success",
          detail: {
            dry_run: true,
            discovered_urns: ["urn:li:dataset:a"],
            discovered_urns_count: 1,
            emitted_urns: [],
            emitted_urns_count: 0,
          },
        }}
      />,
    );
    expect(screen.getByText(/no datasets emitted \(dry run\)/i)).toBeTruthy();
    expect(screen.getByText(/Discovered 1/)).toBeTruthy();
  });

  it("does not crash when detail predates the discovered/emitted keys", () => {
    render(
      <IngestionRunPanel
        mode="ACTIVE_CUSTOM_MANAGED"
        canWrite
        onRun={vi.fn()}
        isRunning={false}
        error={null}
        lastRun={{ run_id: "run-old", status: "success", detail: {} }}
      />,
    );
    expect(screen.getByText(/run-old/)).toBeTruthy();
    // Falls back to a zero-count summary rather than throwing.
    expect(screen.getByText(/Discovered 0/)).toBeTruthy();
  });
});

/**
 * Tests for IngestionRunPanel — run gating by mode/role and error mapping.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Run:
 *   Run is shown only for ACTIVE_CUSTOM_MANAGED; other modes show an
 *   explanatory disabled state. INGESTION_RUNNING / INGESTION_RUN_NOT_APPLICABLE
 *   map to human-readable messages.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { IngestionRunPanel } from "./ingestion-run-panel";
import { ApiError } from "@/lib/api/client";

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

  it("renders the last run detail when present", () => {
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
          detail: { entities_ingested: 5 },
        }}
      />,
    );
    expect(screen.getByText(/run-123/)).toBeTruthy();
    expect(screen.getByText(/entities_ingested/)).toBeTruthy();
  });
});

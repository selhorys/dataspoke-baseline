import { expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockApiFetch } = vi.hoisted(() => ({ mockApiFetch: vi.fn() }));
vi.mock("@/lib/api/client", () => ({ apiFetch: mockApiFetch }));
import { useDatasetList } from "./datasets";

it("serializes dataset_urn for the dataset catalog", async () => {
  mockApiFetch.mockResolvedValue({ datasets: [], total_count: 0 });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children);
  const { result } = renderHook(
    () => useDatasetList({ dataset_urn: "orders", offset: 0, limit: 20 }), { wrapper },
  );
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(mockApiFetch.mock.calls[0][0]).toContain("dataset_urn=orders");
});

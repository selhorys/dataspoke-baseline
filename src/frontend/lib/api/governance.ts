"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  CreateMetricFormValues,
  DatasetFilter,
  MetricDefinition,
  MetricDefinitionListResponse,
  MetricEventListResponse,
  MetricFormValues,
  MetricResultListResponse,
  MetricRunResult,
  ScheduleTier,
} from "@/types/governance";

// ── List metrics ───────────────────────────────────────────────────────────────

interface ListMetricsParams {
  offset?: number;
  limit?: number;
  metric_type?: string;
  mode?: string;
  is_enabled?: boolean;
  sort?: string;
}

function buildListMetricsUrl(params: ListMetricsParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.metric_type) sp.set("metric_type", params.metric_type);
  if (params.mode) sp.set("mode", params.mode);
  if (params.is_enabled !== undefined) sp.set("is_enabled", String(params.is_enabled));
  if (params.sort) sp.set("sort", params.sort);
  const qs = sp.toString();
  return `/spoke/governance/metric${qs ? `?${qs}` : ""}`;
}

export function useGovernanceMetrics(params: ListMetricsParams = {}) {
  return useQuery<MetricDefinitionListResponse>({
    queryKey: ["governance", "metrics", params],
    queryFn: () => apiFetch<MetricDefinitionListResponse>(buildListMetricsUrl(params)),
    meta: { handledInline: true },
  });
}

/** Polls enabled metrics for use on the dashboard. */
export function useEnabledMetrics() {
  return usePoll<MetricDefinitionListResponse>({
    queryKey: ["governance", "metrics", { is_enabled: true, limit: 100 }],
    queryFn: () =>
      apiFetch<MetricDefinitionListResponse>(
        buildListMetricsUrl({ is_enabled: true, limit: 100 }),
      ),
    meta: { handledInline: true },
  });
}

// ── Single metric conf ─────────────────────────────────────────────────────────

export function useMetricConf(metricId: string) {
  return useQuery<MetricDefinition>({
    queryKey: ["governance", "metrics", metricId, "conf"],
    queryFn: () => apiFetch<MetricDefinition>(`/spoke/governance/metric/${metricId}/attr/conf`),
    enabled: !!metricId,
    meta: { handledInline: true },
  });
}

// ── Metric results (timeseries) ────────────────────────────────────────────────

interface MetricResultsParams {
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
  sort?: string;
}

function buildResultsUrl(metricId: string, params: MetricResultsParams): string {
  const sp = new URLSearchParams();
  if (params.from) sp.set("from", params.from);
  if (params.to) sp.set("to", params.to);
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.sort) sp.set("sort", params.sort);
  const qs = sp.toString();
  return `/spoke/governance/metric/${metricId}/attr/result${qs ? `?${qs}` : ""}`;
}

export function useMetricResults(metricId: string, params: MetricResultsParams = {}) {
  return usePoll<MetricResultListResponse>({
    queryKey: ["governance", "metrics", metricId, "results", params],
    queryFn: () => apiFetch<MetricResultListResponse>(buildResultsUrl(metricId, params)),
    enabled: !!metricId,
    meta: { handledInline: true },
  });
}

export function useLatestMetricResult(metricId: string) {
  return useQuery<MetricResultListResponse>({
    queryKey: ["governance", "metrics", metricId, "results", "latest"],
    queryFn: () =>
      apiFetch<MetricResultListResponse>(buildResultsUrl(metricId, { limit: 1 })),
    enabled: !!metricId,
    meta: { handledInline: true },
  });
}

// ── Metric events ──────────────────────────────────────────────────────────────

interface MetricEventParams {
  from?: string;
  to?: string;
  limit?: number;
  sort?: string;
}

function buildMetricEventUrl(metricId: string, params: MetricEventParams): string {
  const sp = new URLSearchParams();
  if (params.from) sp.set("from", params.from);
  if (params.to) sp.set("to", params.to);
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.sort) sp.set("sort", params.sort);
  const qs = sp.toString();
  return `/spoke/governance/metric/${metricId}/event${qs ? `?${qs}` : ""}`;
}

export function useMetricEvents(metricId: string, params: MetricEventParams = {}) {
  return usePoll<MetricEventListResponse>({
    queryKey: ["governance", "metrics", metricId, "events", params],
    queryFn: () =>
      apiFetch<MetricEventListResponse>(buildMetricEventUrl(metricId, params)),
    enabled: !!metricId,
    meta: { handledInline: true },
  });
}

// ── Create metric ──────────────────────────────────────────────────────────────

interface CreateMetricBody {
  metric_id: string;
  mode: string;
  is_enabled: boolean;
  metric_type: string;
  title: string;
  description: string;
  metrics: string[];
  metric_conf: Record<string, unknown>;
  schedule_tier: ScheduleTier | null;
  dataset_filter: DatasetFilter;
}

function toCreateBody(values: CreateMetricFormValues): CreateMetricBody {
  return {
    metric_id: values.metric_id,
    mode: values.mode,
    is_enabled: values.is_enabled,
    metric_type: values.metric_type,
    title: values.title,
    description: values.description,
    metrics: values.metrics,
    metric_conf: values.metric_conf,
    schedule_tier: values.schedule_tier,
    dataset_filter: values.dataset_filter,
  };
}

export function useCreateMetric() {
  const qc = useQueryClient();
  return useMutation<MetricDefinition, Error, CreateMetricFormValues>({
    mutationFn: (values) =>
      apiFetch<MetricDefinition>("/spoke/governance/metric", {
        method: "POST",
        body: JSON.stringify(toCreateBody(values)),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["governance", "metrics"] });
    },
  });
}

// ── Update metric conf (PUT — full replace) ────────────────────────────────────

interface ReplaceMetricBody {
  mode: string;
  is_enabled: boolean;
  metric_type: string;
  title: string;
  description: string;
  metrics: string[];
  metric_conf: Record<string, unknown>;
  schedule_tier: ScheduleTier | null;
  dataset_filter: DatasetFilter;
}

function toReplaceBody(values: MetricFormValues): ReplaceMetricBody {
  return {
    mode: values.mode,
    is_enabled: values.is_enabled,
    metric_type: values.metric_type,
    title: values.title,
    description: values.description,
    metrics: values.metrics,
    metric_conf: values.metric_conf,
    schedule_tier: values.schedule_tier,
    dataset_filter: values.dataset_filter,
  };
}

export function useReplaceMetricConf() {
  const qc = useQueryClient();
  return useMutation<MetricDefinition, Error, { metricId: string; values: MetricFormValues }>({
    mutationFn: ({ metricId, values }) =>
      apiFetch<MetricDefinition>(`/spoke/governance/metric/${metricId}/attr/conf`, {
        method: "PUT",
        body: JSON.stringify(toReplaceBody(values)),
      }),
    meta: { handledInline: true },
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({ queryKey: ["governance", "metrics"] });
      void qc.invalidateQueries({
        queryKey: ["governance", "metrics", vars.metricId, "conf"],
      });
    },
  });
}

// ── Update metric conf (PATCH — partial) ───────────────────────────────────────

/** Partial conf body — a subset of the editable conf definition fields. */
interface PatchMetricBody {
  mode?: string;
  is_enabled?: boolean;
  metric_type?: string;
  title?: string;
  description?: string;
  metrics?: string[];
  metric_conf?: Record<string, unknown>;
  schedule_tier?: ScheduleTier | null;
  dataset_filter?: DatasetFilter;
}

export function useUpdateMetricConf() {
  const qc = useQueryClient();
  return useMutation<MetricDefinition, Error, { metricId: string; patch: PatchMetricBody }>({
    mutationFn: ({ metricId, patch }) =>
      apiFetch<MetricDefinition>(`/spoke/governance/metric/${metricId}/attr/conf`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      }),
    meta: { handledInline: true },
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({ queryKey: ["governance", "metrics"] });
      void qc.invalidateQueries({
        queryKey: ["governance", "metrics", vars.metricId, "conf"],
      });
    },
  });
}

// ── Delete metric conf ─────────────────────────────────────────────────────────

export function useDeleteMetric() {
  const qc = useQueryClient();
  return useMutation<void, Error, { metricId: string }>({
    mutationFn: ({ metricId }) =>
      apiFetch<void>(`/spoke/governance/metric/${metricId}/attr/conf`, {
        method: "DELETE",
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["governance", "metrics"] });
    },
  });
}

// ── Run metric ─────────────────────────────────────────────────────────────────

export function useRunMetric() {
  const qc = useQueryClient();
  return useMutation<MetricRunResult, Error, { metricId: string; dry_run?: boolean }>({
    mutationFn: ({ metricId, dry_run = false }) => {
      const url = `/spoke/governance/metric/${metricId}/method/run${dry_run ? "?dry_run=true" : ""}`;
      return apiFetch<MetricRunResult>(url, { method: "POST" });
    },
    meta: { handledInline: true },
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({
        queryKey: ["governance", "metrics", vars.metricId, "results"],
      });
      void qc.invalidateQueries({
        queryKey: ["governance", "metrics", vars.metricId, "events"],
      });
    },
  });
}

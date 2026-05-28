"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { useCreateMetric } from "@/lib/api/governance";
import { MetricForm } from "@/components/governance/metric-form";
import type { CreateMetricFormValues, MetricFormValues } from "@/types/governance";

const DEFAULT_VALUES: MetricFormValues = {
  mode: "active",
  metric_type: "doc-health",
  title: "",
  description: "",
  metrics: [],
  metric_conf: {},
  schedule_tier: "daily",
  is_enabled: false,
  dataset_filter: {},
};

export default function NewMetricPage() {
  const router = useRouter();
  const { mutate, isPending, error } = useCreateMetric();

  const serverError =
    error instanceof ApiError
      ? `${error.error_code}: ${error.message}`
      : error?.message;

  const handleSubmit = (values: MetricFormValues | CreateMetricFormValues) => {
    mutate(values as CreateMetricFormValues, {
      onSuccess: (created) => {
        router.push(`/governance/metrics/${created.id}`);
      },
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Link
          href="/governance/metrics"
          className="text-muted-foreground hover:text-foreground"
          aria-label="Back to metrics"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">New metric</h1>
      </div>

      <div className="max-w-2xl">
        <MetricForm
          defaultValues={DEFAULT_VALUES}
          isCreate
          onSubmit={handleSubmit}
          onCancel={() => router.push("/governance/metrics")}
          isPending={isPending}
          serverError={serverError}
        />
      </div>
    </div>
  );
}

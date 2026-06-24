"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { useCreateMetric } from "@/lib/api/governance";
import { MetricForm } from "@/components/governance/metric-form";
import { toast } from "@/components/ui/use-toast";
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
  const { mutate, isPending } = useCreateMetric();

  const handleSubmit = (values: MetricFormValues | CreateMetricFormValues) => {
    mutate(values as CreateMetricFormValues, {
      onSuccess: (created) => {
        router.push(`/governance/metrics/${created.id}`);
      },
      onError: (err) => {
        if (err instanceof ApiError) {
          toast({
            variant: "destructive",
            title: err.error_code,
            description: err.message,
          });
        } else {
          toast({
            variant: "destructive",
            title: "Failed to create metric",
            description: err.message,
          });
        }
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

      <div>
        <MetricForm
          defaultValues={DEFAULT_VALUES}
          isCreate
          onSubmit={handleSubmit}
          onCancel={() => router.push("/governance/metrics")}
          isPending={isPending}
        />
      </div>
    </div>
  );
}

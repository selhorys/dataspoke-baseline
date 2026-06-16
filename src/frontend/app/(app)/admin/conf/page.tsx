"use client";

import { useEffect, useState } from "react";
import { useForm, Controller } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMe } from "@/lib/auth/use-me";
import { useRuntimeConf, useUpdateRuntimeConf } from "@/lib/api/admin";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field } from "@/components/forms/field";
import { PasswordInput } from "@/components/forms/password-input";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { confSchema, toFormDefaults, buildPatch } from "./conf-form.schema";
import type { ConfFormValues } from "./conf-form.schema";

// ── Page ──────────────────────────────────────────────────────────────────────

export default function AdminConfPage() {
  const { isAdmin, isLoading: meLoading } = useMe();
  const { data: conf, isLoading: confLoading } = useRuntimeConf();
  const { mutateAsync: updateConf, isPending } = useUpdateRuntimeConf();
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const tz = useDisplayTz();

  const {
    register,
    handleSubmit,
    reset,
    control,
    formState: { errors },
  } = useForm<ConfFormValues>({
    resolver: zodResolver(confSchema),
    defaultValues: {
      llm_provider: "",
      llm_model: "",
      llm_api_key: "",
      ontogen_llm_max_iterations: 3,
      ontogen_debate_max_turns: 4,
      ontogen_debate_rag_k: 5,
      ontogen_debate_reviewer_model: "",
      metagen_llm_max_iterations: 3,
      metagen_debate_max_turns: 4,
      metagen_debate_rag_k: 5,
      metagen_debate_reviewer_model: "",
      metagen_confidence_threshold: 0.7,
      metagen_ontology_rag_node_k: 5,
      metagen_ontology_rag_edge_k: 5,
      metagen_ontology_rag_triple_k: 5,
      validation_score_n_intervals: 3,
      stub_redis_client: false,
      stub_llm_client: false,
      stub_pgvector_manager: false,
      stub_notification_service: false,
      auth_datahub_corp_group: "",
    },
  });

  useEffect(() => {
    if (conf) {
      reset(toFormDefaults(conf));
    }
  }, [conf, reset]);

  async function onSubmit(values: ConfFormValues) {
    if (!conf) return;

    const patch = buildPatch(values, conf);
    if (Object.keys(patch).length === 0) {
      toast({ title: "No changes to save." });
      return;
    }

    try {
      const updated = await updateConf(patch);
      // Reset form with fresh values; keep llm_api_key blank
      reset(toFormDefaults(updated));
      setSavedAt(updated.updated_at);
      toast({ title: "Configuration saved." });
    } catch (err) {
      if (err instanceof ApiError) {
        toast({ variant: "destructive", title: "Save failed", description: err.message });
      } else {
        toast({
          variant: "destructive",
          title: "Save failed",
          description: "An unexpected error occurred.",
        });
      }
    }
  }

  if (meLoading) {
    return <Skeleton className="h-40 w-full" />;
  }

  if (!isAdmin) {
    return (
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Admin — Configurations</h1>
        <p className="text-sm text-muted-foreground">
          You do not have permission to access this page.
        </p>
      </div>
    );
  }

  if (confLoading) {
    return (
      <div className="max-w-2xl space-y-4">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  return (
    <div className="max-w-2xl">
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Admin — Configurations</h1>
      <p className="mb-6 text-sm text-muted-foreground">
        Behavioral tunables, LLM settings, and dependency-stub toggles.
      </p>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
        {/* LLM */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">LLM</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <Field label="Provider" htmlFor="llm_provider" error={errors.llm_provider?.message} required>
              <Input id="llm_provider" {...register("llm_provider")} />
            </Field>
            <Field label="Model" htmlFor="llm_model" error={errors.llm_model?.message} required>
              <Input id="llm_model" {...register("llm_model")} />
            </Field>
            <Field
              label="API Key"
              htmlFor="llm_api_key"
              error={errors.llm_api_key?.message}
              hint="Leave blank to keep current."
            >
              <PasswordInput id="llm_api_key" autoComplete="off" {...register("llm_api_key")} />
            </Field>
          </CardContent>
        </Card>

        {/* Ontology Generation */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Ontology Generation</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-3 gap-4">
              <Field
                label="Max iterations"
                htmlFor="ontogen_llm_max_iterations"
                error={errors.ontogen_llm_max_iterations?.message}
              >
                <Input
                  id="ontogen_llm_max_iterations"
                  type="number"
                  min={1}
                  max={20}
                  {...register("ontogen_llm_max_iterations")}
                />
              </Field>
              <Field
                label="Debate turns"
                htmlFor="ontogen_debate_max_turns"
                error={errors.ontogen_debate_max_turns?.message}
              >
                <Input
                  id="ontogen_debate_max_turns"
                  type="number"
                  min={2}
                  max={10}
                  {...register("ontogen_debate_max_turns")}
                />
              </Field>
              <Field
                label="RAG k"
                htmlFor="ontogen_debate_rag_k"
                error={errors.ontogen_debate_rag_k?.message}
              >
                <Input
                  id="ontogen_debate_rag_k"
                  type="number"
                  min={0}
                  max={20}
                  {...register("ontogen_debate_rag_k")}
                />
              </Field>
            </div>
            <Field
              label="Reviewer model"
              htmlFor="ontogen_debate_reviewer_model"
              error={errors.ontogen_debate_reviewer_model?.message}
              hint="Optional — leave blank to clear."
            >
              <Input id="ontogen_debate_reviewer_model" {...register("ontogen_debate_reviewer_model")} />
            </Field>
          </CardContent>
        </Card>

        {/* Metadata Generation */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Metadata Generation</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-3 gap-4">
              <Field
                label="Max iterations"
                htmlFor="metagen_llm_max_iterations"
                error={errors.metagen_llm_max_iterations?.message}
              >
                <Input
                  id="metagen_llm_max_iterations"
                  type="number"
                  min={1}
                  max={20}
                  {...register("metagen_llm_max_iterations")}
                />
              </Field>
              <Field
                label="Debate turns"
                htmlFor="metagen_debate_max_turns"
                error={errors.metagen_debate_max_turns?.message}
              >
                <Input
                  id="metagen_debate_max_turns"
                  type="number"
                  min={2}
                  max={10}
                  {...register("metagen_debate_max_turns")}
                />
              </Field>
              <Field
                label="RAG k"
                htmlFor="metagen_debate_rag_k"
                error={errors.metagen_debate_rag_k?.message}
              >
                <Input
                  id="metagen_debate_rag_k"
                  type="number"
                  min={0}
                  max={20}
                  {...register("metagen_debate_rag_k")}
                />
              </Field>
            </div>
            <Field
              label="Reviewer model"
              htmlFor="metagen_debate_reviewer_model"
              error={errors.metagen_debate_reviewer_model?.message}
              hint="Optional — leave blank to clear."
            >
              <Input id="metagen_debate_reviewer_model" {...register("metagen_debate_reviewer_model")} />
            </Field>
            <div className="grid grid-cols-2 gap-4">
              <Field
                label="Confidence threshold"
                htmlFor="metagen_confidence_threshold"
                error={errors.metagen_confidence_threshold?.message}
              >
                <Input
                  id="metagen_confidence_threshold"
                  type="number"
                  step="0.01"
                  min={0}
                  max={1}
                  {...register("metagen_confidence_threshold")}
                />
              </Field>
            </div>
            <p className="text-sm font-medium">Ontology RAG</p>
            <div className="grid grid-cols-3 gap-4">
              <Field
                label="Node k"
                htmlFor="metagen_ontology_rag_node_k"
                error={errors.metagen_ontology_rag_node_k?.message}
              >
                <Input
                  id="metagen_ontology_rag_node_k"
                  type="number"
                  min={0}
                  max={20}
                  {...register("metagen_ontology_rag_node_k")}
                />
              </Field>
              <Field
                label="Edge k"
                htmlFor="metagen_ontology_rag_edge_k"
                error={errors.metagen_ontology_rag_edge_k?.message}
              >
                <Input
                  id="metagen_ontology_rag_edge_k"
                  type="number"
                  min={0}
                  max={20}
                  {...register("metagen_ontology_rag_edge_k")}
                />
              </Field>
              <Field
                label="Triple k"
                htmlFor="metagen_ontology_rag_triple_k"
                error={errors.metagen_ontology_rag_triple_k?.message}
              >
                <Input
                  id="metagen_ontology_rag_triple_k"
                  type="number"
                  min={0}
                  max={20}
                  {...register("metagen_ontology_rag_triple_k")}
                />
              </Field>
            </div>
          </CardContent>
        </Card>

        {/* Validation */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Validation</CardTitle>
          </CardHeader>
          <CardContent>
            <Field
              label="Score intervals"
              htmlFor="validation_score_n_intervals"
              error={errors.validation_score_n_intervals?.message}
            >
              <Input
                id="validation_score_n_intervals"
                type="number"
                min={1}
                className="max-w-[120px]"
                {...register("validation_score_n_intervals")}
              />
            </Field>
          </CardContent>
        </Card>

        {/* Dependency stubs */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Dependency stubs</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              <Controller
                name="stub_redis_client"
                control={control}
                render={({ field }) => (
                  <label className="flex cursor-pointer items-center gap-3">
                    <Checkbox
                      id="stub_redis_client"
                      checked={field.value}
                      onCheckedChange={(v) => field.onChange(v === true)}
                    />
                    <span className="text-sm">Redis client stub</span>
                  </label>
                )}
              />
              <Controller
                name="stub_llm_client"
                control={control}
                render={({ field }) => (
                  <label className="flex cursor-pointer items-center gap-3">
                    <Checkbox
                      id="stub_llm_client"
                      checked={field.value}
                      onCheckedChange={(v) => field.onChange(v === true)}
                    />
                    <span className="text-sm">LLM client stub</span>
                  </label>
                )}
              />
              <Controller
                name="stub_pgvector_manager"
                control={control}
                render={({ field }) => (
                  <label className="flex cursor-pointer items-center gap-3">
                    <Checkbox
                      id="stub_pgvector_manager"
                      checked={field.value}
                      onCheckedChange={(v) => field.onChange(v === true)}
                    />
                    <span className="text-sm">pgvector manager stub</span>
                  </label>
                )}
              />
              <Controller
                name="stub_notification_service"
                control={control}
                render={({ field }) => (
                  <label className="flex cursor-pointer items-center gap-3">
                    <Checkbox
                      id="stub_notification_service"
                      checked={field.value}
                      onCheckedChange={(v) => field.onChange(v === true)}
                    />
                    <span className="text-sm">Notification service stub</span>
                  </label>
                )}
              />
            </div>
          </CardContent>
        </Card>

        {/* Auth */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Auth</CardTitle>
          </CardHeader>
          <CardContent>
            <Field
              label="DataHub corp group"
              htmlFor="auth_datahub_corp_group"
              error={errors.auth_datahub_corp_group?.message}
              required
            >
              <Input id="auth_datahub_corp_group" {...register("auth_datahub_corp_group")} />
            </Field>
          </CardContent>
        </Card>

        {/* Footer */}
        <div className="flex items-center gap-4">
          <Button type="submit" disabled={isPending}>
            {isPending ? "Saving..." : "Save changes"}
          </Button>
          {savedAt && (
            <p className="text-sm text-muted-foreground">
              Saved · updated {formatDateTime(savedAt, tz)}
            </p>
          )}
        </div>
      </form>
    </div>
  );
}

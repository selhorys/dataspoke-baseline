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
import { FormGrid } from "@/components/ui/form-grid";
import { Field } from "@/components/forms/field";
import { PasswordInput } from "@/components/forms/password-input";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { confSchema, toFormDefaults, buildPatch } from "./conf-form.schema";
import type { ConfFormValues } from "./conf-form.schema";
import { WorkflowSchedulesCard } from "./workflow-schedules-card";

const ADMIN_CONF_FORM_ID = "admin-conf-form";

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
      <div className="space-y-4">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="mb-1 text-2xl font-semibold tracking-tight">Admin — Configurations</h1>
          <p className="text-sm text-muted-foreground">
            Behavioral tunables, LLM settings, and dependency-stub toggles.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-4">
          {savedAt && (
            <p className="text-sm text-muted-foreground">
              Saved · updated {formatDateTime(savedAt, tz)}
            </p>
          )}
          <Button type="submit" form={ADMIN_CONF_FORM_ID} disabled={isPending}>
            {isPending ? "Saving..." : "Save changes"}
          </Button>
        </div>
      </div>

      <form id={ADMIN_CONF_FORM_ID} onSubmit={handleSubmit(onSubmit)} className="space-y-6">
        {/* LLM */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">LLM</CardTitle>
          </CardHeader>
          <CardContent>
            <FormGrid>
              <Field
                label="Provider"
                htmlFor="llm_provider"
                description="LLM vendor used for inference and embeddings. Available: gemini (default) or openai."
                error={errors.llm_provider?.message}
                required
              >
                <Input id="llm_provider" {...register("llm_provider")} />
              </Field>
              <Field
                label="Model"
                htmlFor="llm_model"
                description="Chat/inference model id for the Producer and Reviewer — a standard model name for the selected provider, passed to its LangChain chat client (e.g. gemini-3.5-flash, gpt-4o). The embedding model is fixed per provider. Default gemini-3.5-flash."
                error={errors.llm_model?.message}
                required
              >
                <Input id="llm_model" {...register("llm_model")} />
              </Field>
              <Field
                label="API Key"
                htmlFor="llm_api_key"
                description="Written to separated secure storage (currently a K8s Secret. customize it!), never stored in the database. Leave blank to keep current."
                error={errors.llm_api_key?.message}
                className="sm:col-span-2"
              >
                <PasswordInput id="llm_api_key" autoComplete="off" {...register("llm_api_key")} />
              </Field>
            </FormGrid>
          </CardContent>
        </Card>

        {/* Ontology Generation */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Ontology Generation</CardTitle>
          </CardHeader>
          <CardContent>
            <FormGrid>
              <Field
                label="Max iterations"
                htmlFor="ontogen_llm_max_iterations"
                description="Producer inference-loop cap; one iteration = one model invocation (1–20, default 3)."
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
                description="Producer↔Reviewer adversarial-debate turn cap (2–10, default 4)."
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
                description="Approved-anchor items pgvector-sampled to ground the Reviewer (0–20, 0 disables)."
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
              <Field
                label="Reviewer model"
                htmlFor="ontogen_debate_reviewer_model"
                description="Optional separate model id (same format as Model) for the Reviewer role; blank uses the main model."
                error={errors.ontogen_debate_reviewer_model?.message}                className="sm:col-span-2"
              >
                <Input id="ontogen_debate_reviewer_model" {...register("ontogen_debate_reviewer_model")} />
              </Field>
            </FormGrid>
          </CardContent>
        </Card>

        {/* Metadata Generation */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Metadata Generation</CardTitle>
          </CardHeader>
          <CardContent>
            <FormGrid>
              <Field
                label="Max iterations"
                htmlFor="metagen_llm_max_iterations"
                description="Producer inference-loop cap; one iteration = one model invocation (1–20, default 3)."
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
                description="Producer↔Reviewer adversarial-debate turn cap (2–10, default 4)."
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
                description="Approved-anchor items pgvector-sampled to ground the Reviewer (0–20, 0 disables)."
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
              <Field
                label="Reviewer model"
                htmlFor="metagen_debate_reviewer_model"
                description="Optional separate model id (same format as Model) for the Reviewer role; blank uses the main model."
                error={errors.metagen_debate_reviewer_model?.message}                className="sm:col-span-2"
              >
                <Input id="metagen_debate_reviewer_model" {...register("metagen_debate_reviewer_model")} />
              </Field>
              <Field
                label="Confidence threshold"
                htmlFor="metagen_confidence_threshold"
                description="Minimum candidate confidence required to persist a generated suggestion (0.0–1.0, default 0.7)."
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
              <div className="sm:col-span-2">
                <p className="text-sm font-medium">Ontology RAG</p>
                <p className="text-xs text-muted-foreground">
                  Approved-ontology items retrieved as Producer-evidence grounding for metadata
                  generation (0 disables each).
                </p>
              </div>
              <Field
                label="Node k"
                htmlFor="metagen_ontology_rag_node_k"
                description="Ontology nodes retrieved as grounding context (0–20, default 5)."
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
                description="Ontology edges retrieved as grounding context (0–20, default 5)."
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
                description="Ontology triples retrieved as grounding context (0–20, default 5)."
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
            </FormGrid>
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
              description="Recent result inter-arrival gaps used to size the validation-score metric window (min 1, default 3)."
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
                  <div>
                    <label className="flex cursor-pointer items-center gap-3">
                      <Checkbox
                        id="stub_redis_client"
                        checked={field.value}
                        onCheckedChange={(v) => field.onChange(v === true)}
                      />
                      <span className="text-sm">Redis client stub</span>
                    </label>
                    <p className="ml-7 text-xs text-muted-foreground">
                      Replaces Redis with an in-process stub whose cache, lock, and pub/sub ops are
                      no-ops; leave off in normal operation.
                    </p>
                  </div>
                )}
              />
              <Controller
                name="stub_llm_client"
                control={control}
                render={({ field }) => (
                  <div>
                    <label className="flex cursor-pointer items-center gap-3">
                      <Checkbox
                        id="stub_llm_client"
                        checked={field.value}
                        onCheckedChange={(v) => field.onChange(v === true)}
                      />
                      <span className="text-sm">LLM client stub</span>
                    </label>
                    <p className="ml-7 text-xs text-muted-foreground">
                      Replaces the LLM client with an in-process stub returning deterministic,
                      schema-valid payloads and no provider calls; leave off in normal operation.
                    </p>
                  </div>
                )}
              />
              <Controller
                name="stub_pgvector_manager"
                control={control}
                render={({ field }) => (
                  <div>
                    <label className="flex cursor-pointer items-center gap-3">
                      <Checkbox
                        id="stub_pgvector_manager"
                        checked={field.value}
                        onCheckedChange={(v) => field.onChange(v === true)}
                      />
                      <span className="text-sm">pgvector manager stub</span>
                    </label>
                    <p className="ml-7 text-xs text-muted-foreground">
                      Replaces the pgvector manager with an in-process stub whose similarity search
                      returns nothing, disabling RAG retrieval; leave off in normal operation.
                    </p>
                  </div>
                )}
              />
              <Controller
                name="stub_notification_service"
                control={control}
                render={({ field }) => (
                  <div>
                    <label className="flex cursor-pointer items-center gap-3">
                      <Checkbox
                        id="stub_notification_service"
                        checked={field.value}
                        onCheckedChange={(v) => field.onChange(v === true)}
                      />
                      <span className="text-sm">Notification service stub</span>
                    </label>
                    <p className="ml-7 text-xs text-muted-foreground">
                      Replaces the notification service with an in-process stub that sends no emails
                      or alerts; leave off in normal operation.
                    </p>
                  </div>
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
              description="DataHub corp group whose membership is recognized for access (default dataspoke-users)."
              error={errors.auth_datahub_corp_group?.message}
              required
            >
              <Input id="auth_datahub_corp_group" {...register("auth_datahub_corp_group")} />
            </Field>
          </CardContent>
        </Card>

      </form>

      {/* Workflow schedules — operational DAG paused-state control (Airflow), a
          self-contained section OUTSIDE the runtime-conf form with its own
          per-toggle immediate PATCH, not the form's single Save. */}
      <div className="mt-6">
        <WorkflowSchedulesCard />
      </div>
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMe } from "@/lib/auth/use-me";
import {
  useDatahubPeripheral,
  useUpdateDatahubPeripheral,
  useLangfusePeripheral,
  useUpdateLangfusePeripheral,
} from "@/lib/api/admin";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FormGrid } from "@/components/ui/form-grid";
import { Field } from "@/components/forms/field";
import { PasswordInput } from "@/components/forms/password-input";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { DatahubPeripheral, LangfusePeripheral } from "@/lib/api/types";
import {
  datahubSchema,
  datahubToFormDefaults,
  datahubBuildPatch,
  langfuseSchema,
  langfuseToFormDefaults,
  langfuseBuildPatch,
} from "./peripherals-form.schema";
import type { DatahubFormValues, LangfuseFormValues } from "./peripherals-form.schema";

const DATAHUB_FORM_ID = "admin-peripheral-datahub-form";
const LANGFUSE_FORM_ID = "admin-peripheral-langfuse-form";

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "An unexpected error occurred.";
}

// ── DataHub card ────────────────────────────────────────────────────────────────

function DatahubCard({ peripheral }: { peripheral: DatahubPeripheral }) {
  const { mutateAsync: updateDatahub, isPending } = useUpdateDatahubPeripheral();
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const tz = useDisplayTz();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<DatahubFormValues>({
    resolver: zodResolver(datahubSchema),
    defaultValues: datahubToFormDefaults(peripheral),
  });

  useEffect(() => {
    reset(datahubToFormDefaults(peripheral));
  }, [peripheral, reset]);

  async function onSubmit(values: DatahubFormValues) {
    const patch = datahubBuildPatch(values, peripheral);
    if (Object.keys(patch).length === 0) {
      toast({ title: "No changes to save." });
      return;
    }
    try {
      const updated = await updateDatahub(patch);
      reset(datahubToFormDefaults(updated));
      setSavedAt(updated.updated_at);
      toast({ title: "DataHub configuration saved." });
    } catch (err) {
      toast({ variant: "destructive", title: "Save failed", description: describeError(err) });
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <CardTitle className="text-base">DataHub</CardTitle>
        {savedAt && (
          <p className="text-xs text-muted-foreground">
            Saved · updated {formatDateTime(savedAt, tz)}
          </p>
        )}
      </CardHeader>
      <CardContent>
        <form id={DATAHUB_FORM_ID} onSubmit={handleSubmit(onSubmit)}>
          <FormGrid>
            <Field
              label="GMS URL"
              htmlFor="datahub_gms_url"
              description="DataHub GMS (metadata service) base URL used for REST + GraphQL calls."
              error={errors.gms_url?.message}
            >
              <Input id="datahub_gms_url" {...register("gms_url")} />
            </Field>
            <Field
              label="Kafka brokers"
              htmlFor="datahub_kafka_brokers"
              description="Bootstrap servers for the DataHub MCP/MCE Kafka topics (host:port, comma-separated)."
              error={errors.kafka_brokers?.message}
            >
              <Input id="datahub_kafka_brokers" {...register("kafka_brokers")} />
            </Field>
            <Field
              label="Token"
              htmlFor="datahub_token"
              description="DataHub personal access token. Written to separated secure storage (currently a K8s Secret), never stored in the database. Leave blank to keep current."
              error={errors.token?.message}
              className="sm:col-span-2"
            >
              <PasswordInput id="datahub_token" autoComplete="off" {...register("token")} />
            </Field>
            <Field
              label="Service corpuser URN"
              htmlFor="datahub_service_corpuser_urn"
              description="DataHub corpuser URN credited as the actor for DataSpoke-emitted assertions and ingestion (e.g. urn:li:corpuser:dataspoke)."
              error={errors.service_corpuser_urn?.message}
            >
              <Input
                id="datahub_service_corpuser_urn"
                {...register("service_corpuser_urn")}
              />
            </Field>
            <Field
              label="Default env"
              htmlFor="datahub_default_env"
              description="Fabric/environment applied to ingested datasets when a recipe omits env (e.g. PROD, DEV, QA, TEST)."
              error={errors.default_env?.message}
            >
              <Input id="datahub_default_env" {...register("default_env")} />
            </Field>
          </FormGrid>
          <div className="mt-4 flex justify-end">
            <Button type="submit" form={DATAHUB_FORM_ID} disabled={isPending}>
              {isPending ? "Saving..." : "Save DataHub"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ── Langfuse card ───────────────────────────────────────────────────────────────

function LangfuseCard({ peripheral }: { peripheral: LangfusePeripheral }) {
  const { mutateAsync: updateLangfuse, isPending } = useUpdateLangfusePeripheral();
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const tz = useDisplayTz();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<LangfuseFormValues>({
    resolver: zodResolver(langfuseSchema),
    defaultValues: langfuseToFormDefaults(peripheral),
  });

  useEffect(() => {
    reset(langfuseToFormDefaults(peripheral));
  }, [peripheral, reset]);

  async function onSubmit(values: LangfuseFormValues) {
    const patch = langfuseBuildPatch(values, peripheral);
    if (Object.keys(patch).length === 0) {
      toast({ title: "No changes to save." });
      return;
    }
    try {
      const updated = await updateLangfuse(patch);
      reset(langfuseToFormDefaults(updated));
      setSavedAt(updated.updated_at);
      toast({ title: "Langfuse configuration saved." });
    } catch (err) {
      toast({ variant: "destructive", title: "Save failed", description: describeError(err) });
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <CardTitle className="text-base">Langfuse</CardTitle>
        {savedAt && (
          <p className="text-xs text-muted-foreground">
            Saved · updated {formatDateTime(savedAt, tz)}
          </p>
        )}
      </CardHeader>
      <CardContent>
        <form id={LANGFUSE_FORM_ID} onSubmit={handleSubmit(onSubmit)}>
          <FormGrid>
            <Field
              label="Host"
              htmlFor="langfuse_host"
              description="Self-hosted Langfuse base URL for LLM-observability tracing. Absence disables tracing."
              error={errors.host?.message}
            >
              <Input id="langfuse_host" {...register("host")} />
            </Field>
            <Field
              label="Public key"
              htmlFor="langfuse_public_key"
              description="Langfuse project public key (pk-...)."
              error={errors.public_key?.message}
            >
              <Input id="langfuse_public_key" {...register("public_key")} />
            </Field>
            <Field
              label="Secret key"
              htmlFor="langfuse_secret_key"
              description="Langfuse project secret key (sk-...). Written to separated secure storage (currently a K8s Secret), never stored in the database. Leave blank to keep current."
              error={errors.secret_key?.message}
              className="sm:col-span-2"
            >
              <PasswordInput
                id="langfuse_secret_key"
                autoComplete="off"
                {...register("secret_key")}
              />
            </Field>
            <Field
              label="Project ID"
              htmlFor="langfuse_project_id"
              description="Langfuse project slug recorded on emitted traces as metadata."
              error={errors.project_id?.message}
            >
              <Input id="langfuse_project_id" {...register("project_id")} />
            </Field>
            <Field
              label="Environment tag"
              htmlFor="langfuse_environment_tag"
              description="Value passed as the Langfuse trace environment (e.g. production, staging, development)."
              error={errors.environment_tag?.message}
            >
              <Input
                id="langfuse_environment_tag"
                {...register("environment_tag")}
              />
            </Field>
          </FormGrid>
          <div className="mt-4 flex justify-end">
            <Button type="submit" form={LANGFUSE_FORM_ID} disabled={isPending}>
              {isPending ? "Saving..." : "Save Langfuse"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function AdminPeripheralsPage() {
  const { isAdmin, isLoading: meLoading } = useMe();
  const { data: datahub, isLoading: datahubLoading } = useDatahubPeripheral();
  const { data: langfuse, isLoading: langfuseLoading } = useLangfusePeripheral();

  if (meLoading) {
    return <Skeleton className="h-40 w-full" />;
  }

  if (!isAdmin) {
    return (
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Admin — Peripherals</h1>
        <p className="text-sm text-muted-foreground">
          You do not have permission to access this page.
        </p>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="mb-1 text-2xl font-semibold tracking-tight">Admin — Peripherals</h1>
        <p className="text-sm text-muted-foreground">
          DataHub and Langfuse connection settings. Each card saves independently.
        </p>
      </div>

      <div className="space-y-6">
        {datahubLoading || !datahub ? (
          <Skeleton className="h-64 w-full" />
        ) : (
          <DatahubCard peripheral={datahub} />
        )}
        {langfuseLoading || !langfuse ? (
          <Skeleton className="h-64 w-full" />
        ) : (
          <LangfuseCard peripheral={langfuse} />
        )}
      </div>
    </div>
  );
}

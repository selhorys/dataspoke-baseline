"use client";

/**
 * IngestionConfForm — create or edit an ingestion config.
 *
 * Mode-gating:
 *   active-custom → shows locator, auth, schedule_tier fields; enables Save
 *   passive       → hides locator, auth, schedule_tier; shows DataHub deep link
 *
 * Props:
 *   defaultValues  — initial form values (use defaultFormValues() for a blank create form)
 *   onSubmit       — called with the serialized API request body
 *   onCancel       — optional cancel handler
 *   isPending      — loading state on Save button
 *   serverError?   — top-level error message from the mutation
 */

import { useCallback, useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Field } from "@/components/forms/field";
import { ErrorText } from "@/components/forms/error-text";
import type { IngestionConfFormValues, Platform } from "@/types/ingestion";
import { PLATFORMS_WITH_AUTH, RDBMS_PLATFORMS } from "@/types/ingestion";
import { ingestionConfSchema, fromInternal } from "./ingestion-conf-form.schema";

interface IngestionConfFormProps {
  defaultValues: IngestionConfFormValues;
  onSubmit: (body: Record<string, unknown>) => void;
  onCancel?: () => void;
  isPending: boolean;
  serverError?: string;
  datahubUrl?: string;
}

export function IngestionConfForm({
  defaultValues,
  onSubmit,
  onCancel,
  isPending,
  serverError,
  datahubUrl,
}: IngestionConfFormProps) {
  const {
    register,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
    reset,
  } = useForm<IngestionConfFormValues>({
    resolver: zodResolver(ingestionConfSchema),
    defaultValues,
  });

  useEffect(() => {
    reset(defaultValues);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultValues.mode, defaultValues.platform]);

  const mode = watch("mode");
  const platform = watch("platform") as Platform;
  const isEnabled = watch("is_enabled");

  const isActive = mode === "active-custom";
  const isPassive = mode === "passive";
  const needsAuth = isActive && PLATFORMS_WITH_AUTH.includes(platform);
  const isRdbms = RDBMS_PLATFORMS.includes(platform);
  const isKafka = platform === "kafka";
  const isBigQuery = platform === "bigquery";
  const isSnowflake = platform === "snowflake";

  // When mode changes, clear active-only fields for passive
  const handleModeChange = useCallback(
    (newMode: "active-custom" | "passive") => {
      setValue("mode", newMode, { shouldDirty: true });
      if (newMode === "passive") {
        setValue("schedule_tier", "", { shouldDirty: true });
      }
    },
    [setValue],
  );

  const onValid = (data: IngestionConfFormValues) => {
    onSubmit(fromInternal(data));
  };

  const datahubIngestionUrl = datahubUrl ? `${datahubUrl}/ingestion` : null;

  return (
    <form onSubmit={handleSubmit(onValid)} className="space-y-5">
      {/* mode */}
      <Field label="mode" htmlFor="mode-active-custom" error={errors.mode?.message}>
        <div className="flex gap-4">
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="radio"
              {...register("mode")}
              value="active-custom"
              id="mode-active-custom"
              className="accent-primary"
              onChange={() => handleModeChange("active-custom")}
            />
            active-custom
          </label>
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="radio"
              {...register("mode")}
              value="passive"
              id="mode-passive"
              className="accent-primary"
              onChange={() => handleModeChange("passive")}
            />
            passive
          </label>
        </div>
      </Field>

      {/* platform */}
      <Field
        label="platform"
        htmlFor="platform"
        error={errors.platform?.message}
        required
      >
        <Select
          value={platform}
          onValueChange={(v) => setValue("platform", v as Platform, { shouldDirty: true })}
        >
          <SelectTrigger id="platform">
            <SelectValue placeholder="Select platform" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="postgres">postgres</SelectItem>
            <SelectItem value="mysql">mysql</SelectItem>
            <SelectItem value="oracle">oracle</SelectItem>
            <SelectItem value="bigquery">bigquery</SelectItem>
            <SelectItem value="snowflake">snowflake</SelectItem>
            <SelectItem value="kafka">kafka</SelectItem>
          </SelectContent>
        </Select>
      </Field>

      {/* locator — active-custom only */}
      {isActive && isRdbms && (
        <>
          <Field
            label="locator.host"
            htmlFor="locator-host"
            error={errors.locator_host?.message}
            required
          >
            <Input
              id="locator-host"
              {...register("locator_host")}
              placeholder="db.example.com"
            />
          </Field>
          <Field
            label="locator.port"
            htmlFor="locator-port"
            error={errors.locator_port?.message}
            required
          >
            <Input
              id="locator-port"
              type="number"
              {...register("locator_port")}
              placeholder="5432"
            />
          </Field>
        </>
      )}
      {isActive && isKafka && (
        <Field
          label="locator.bootstrap_servers"
          htmlFor="locator-bootstrap-servers"
          error={errors.locator_bootstrap_servers?.message}
          required
        >
          <Input
            id="locator-bootstrap-servers"
            {...register("locator_bootstrap_servers")}
            placeholder="kafka:9092"
          />
        </Field>
      )}
      {isActive && isBigQuery && (
        <Field
          label="locator.project_id"
          htmlFor="locator-project-id"
          error={errors.locator_project_id?.message}
          required
        >
          <Input
            id="locator-project-id"
            {...register("locator_project_id")}
            placeholder="my-gcp-project"
          />
        </Field>
      )}
      {isActive && isSnowflake && (
        <Field
          label="locator.account_id"
          htmlFor="locator-account-id"
          error={errors.locator_account_id?.message}
          required
        >
          <Input
            id="locator-account-id"
            {...register("locator_account_id")}
            placeholder="abc12345"
          />
        </Field>
      )}

      {/* identifier — all modes */}
      {(isRdbms || isSnowflake) && (
        <>
          <Field
            label="identifier.database"
            htmlFor="identifier-database"
            error={errors.identifier_database?.message}
            required
          >
            <Input
              id="identifier-database"
              {...register("identifier_database")}
              placeholder="mydb"
            />
          </Field>
          <Field
            label="identifier.schema_name"
            htmlFor="identifier-schema-name"
            error={errors.identifier_schema_name?.message}
            required
          >
            <Input
              id="identifier-schema-name"
              {...register("identifier_schema_name")}
              placeholder="public"
            />
          </Field>
          <Field
            label="identifier.table"
            htmlFor="identifier-table"
            error={errors.identifier_table?.message}
            required
          >
            <Input
              id="identifier-table"
              {...register("identifier_table")}
              placeholder="orders"
            />
          </Field>
        </>
      )}
      {isBigQuery && (
        <>
          <Field
            label="identifier.dataset"
            htmlFor="identifier-dataset"
            error={errors.identifier_dataset?.message}
            required
          >
            <Input
              id="identifier-dataset"
              {...register("identifier_dataset")}
              placeholder="analytics"
            />
          </Field>
          <Field
            label="identifier.table"
            htmlFor="identifier-table"
            error={errors.identifier_table?.message}
            required
          >
            <Input
              id="identifier-table"
              {...register("identifier_table")}
              placeholder="events"
            />
          </Field>
        </>
      )}
      {isKafka && (
        <>
          <Field
            label="identifier.topic"
            htmlFor="identifier-topic"
            error={errors.identifier_topic?.message}
            required
          >
            <Input
              id="identifier-topic"
              {...register("identifier_topic")}
              placeholder="user-events"
            />
          </Field>
          <Field
            label="identifier.cluster"
            htmlFor="identifier-cluster"
            error={errors.identifier_cluster?.message}
            required
          >
            <Input
              id="identifier-cluster"
              {...register("identifier_cluster")}
              placeholder="prod"
            />
          </Field>
        </>
      )}

      {/* auth — active-custom + CredentialAuth platforms only */}
      {needsAuth && (
        <>
          <Field
            label="auth.username"
            htmlFor="auth-username"
            error={errors.auth_username?.message}
            required
          >
            <Input
              id="auth-username"
              {...register("auth_username")}
              placeholder="readonly"
            />
          </Field>
          <Field
            label="auth.password"
            htmlFor="auth-password"
            error={errors.auth_password?.message}
            hint="Vault path: supply a plaintext password to write to the Kubernetes Secret. Leave blank to reference a pre-existing Secret."
          >
            <Input
              id="auth-password"
              type="password"
              {...register("auth_password")}
              placeholder="(optional — vault path only)"
            />
          </Field>
          <Field
            label="auth.secret_ref.name"
            htmlFor="auth-secret-ref-name"
            error={errors.auth_secret_ref_name?.message}
            required
            hint="Kubernetes Secret name (must start with 'dataspoke-source-cred-')"
          >
            <Input
              id="auth-secret-ref-name"
              {...register("auth_secret_ref_name")}
              placeholder="dataspoke-source-cred-mydb-creds"
            />
          </Field>
          <Field
            label="auth.secret_ref.key"
            htmlFor="auth-secret-ref-key"
            error={errors.auth_secret_ref_key?.message}
            required
          >
            <Input
              id="auth-secret-ref-key"
              {...register("auth_secret_ref_key")}
              placeholder="password"
            />
          </Field>
        </>
      )}

      {/* is_enabled */}
      <div className="flex items-center gap-2">
        <Checkbox
          id="is-enabled"
          checked={isEnabled}
          onCheckedChange={(checked) =>
            setValue("is_enabled", !!checked, { shouldDirty: true })
          }
        />
        <Label htmlFor="is-enabled" className="cursor-pointer text-sm">
          is_enabled
        </Label>
      </div>

      {/* schedule_tier — active-custom only */}
      {isActive && (
        <Field
          label="schedule_tier"
          htmlFor="schedule-tier"
          error={errors.schedule_tier?.message as string | undefined}
        >
          <Select
            value={watch("schedule_tier") ?? ""}
            onValueChange={(v) =>
              setValue(
                "schedule_tier",
                v as "hourly" | "daily" | "weekly" | "",
                { shouldDirty: true },
              )
            }
          >
            <SelectTrigger id="schedule-tier">
              <SelectValue placeholder="On-demand only" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="">On-demand only</SelectItem>
              <SelectItem value="hourly">hourly</SelectItem>
              <SelectItem value="daily">daily</SelectItem>
              <SelectItem value="weekly">weekly</SelectItem>
            </SelectContent>
          </Select>
        </Field>
      )}

      {/* passive DataHub deep link */}
      {isPassive && (
        <div className="flex items-center gap-2 rounded-md border border-border bg-muted/50 p-3 text-sm">
          <span className="text-muted-foreground">
            Passive — ingestion runs are configured externally.
          </span>
          {datahubIngestionUrl ? (
            <a
              href={datahubIngestionUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-auto inline-flex items-center gap-1 text-primary hover:underline"
            >
              Configure ingestion in DataHub
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          ) : (
            <span className="ml-auto text-muted-foreground">
              (Set DATASPOKE_DATAHUB_URL to enable the DataHub deep link)
            </span>
          )}
        </div>
      )}

      {serverError && <ErrorText message={serverError} />}

      <div className="flex justify-end gap-2">
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel} disabled={isPending}>
            Cancel
          </Button>
        )}
        <Button type="submit" disabled={isPending}>
          {isPending ? "Saving..." : "Save"}
        </Button>
      </div>
    </form>
  );
}

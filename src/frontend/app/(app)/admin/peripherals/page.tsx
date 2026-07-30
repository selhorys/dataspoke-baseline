"use client";

import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { AlertTriangle, Info } from "lucide-react";
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
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FormGrid } from "@/components/ui/form-grid";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Field } from "@/components/forms/field";
import { PasswordInput } from "@/components/forms/password-input";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type {
  DatahubPeripheral,
  KafkaSaslMechanism,
  KafkaSecurityProtocol,
  LangfusePeripheral,
  PeripheralHealth,
} from "@/lib/api/types";
import {
  datahubSchema,
  datahubToFormDefaults,
  datahubBuildPatch,
  isCredentialMechanism,
  isSaslProtocol,
  mechanismOptionsFor,
  KAFKA_SECURITY_PROTOCOLS,
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

/**
 * One of the two transports DataSpoke reaches DataHub over.
 *
 * The planes differ only in their operator-facing label, their DOM id stem, and
 * why they are legitimately `unknown` — the status vocabulary and the rendering
 * are identical, so `PeripheralHealthBadge` serves both.
 */
interface HealthPlane {
  /** Names the transport, so an operator can tell the two badges apart. */
  label: string;
  /** Id stem; each plane owns its own ids so the two badges are separately addressable. */
  idPrefix: string;
  /** Why this plane reports `unknown` on a stock install — a different reason per plane. */
  unknownReason: string;
}

/** `health` — written by the DataHub event consumer. */
const EVENT_STREAM_PLANE: HealthPlane = {
  label: "Event stream",
  idPrefix: "datahub_health",
  unknownReason:
    "No event consumer has reported yet — none is deployed by default, so this is the ordinary reading.",
};

/** `api_health` — written by the hourly DataHub sync sweep. */
const METADATA_API_PLANE: HealthPlane = {
  label: "Metadata API",
  idPrefix: "datahub_api_health",
  unknownReason:
    "The hourly datahub-sync sweep has not reported yet — its DAG ships paused, so this is the ordinary reading.",
};

/**
 * Read-only health badge for one DataHub transport, rendered in the card header.
 *
 * `is_configured` reports presence, not correctness: a wrong mechanism or an
 * expired credential is indistinguishable from a working setup until a reporter
 * actually tries to connect. Both reporters are opt-in, so `unknown` is neutral
 * on either plane rather than a fault — the copy names the plane's own reason.
 *
 * Stateless and fully prop-driven: the component holds nothing across renders,
 * so the two instances cannot leak one plane's status into the other.
 */
function PeripheralHealthBadge({
  plane,
  health,
}: {
  plane: HealthPlane;
  health: PeripheralHealth;
}) {
  const tz = useDisplayTz();
  const statusId = `${plane.idPrefix}_status`;

  if (health.status === "ok") {
    return (
      <div className="text-right">
        <Badge id={statusId} data-status="ok" variant="success">
          {plane.label} OK
        </Badge>
        {health.last_ok_at && (
          <p className="mt-1 text-xs text-muted-foreground">
            Last OK {formatDateTime(health.last_ok_at, tz)}
          </p>
        )}
      </div>
    );
  }

  if (health.status === "error") {
    return (
      <div className="max-w-md text-right">
        <Badge id={statusId} data-status="error" variant="destructive">
          <AlertTriangle className="mr-1 h-3 w-3" aria-hidden="true" />
          {plane.label} error
        </Badge>
        {health.last_error && (
          <p
            id={`${plane.idPrefix}_error`}
            className="mt-1 break-words text-xs text-destructive"
          >
            {health.last_error}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="max-w-md text-right">
      <Badge id={statusId} data-status="unknown" variant="outline">
        {plane.label} status unknown
      </Badge>
      <p id={`${plane.idPrefix}_unknown_reason`} className="mt-1 text-xs text-muted-foreground">
        {plane.unknownReason}
      </p>
    </div>
  );
}

function DatahubCard({ peripheral }: { peripheral: DatahubPeripheral }) {
  const { mutateAsync: updateDatahub, isPending } = useUpdateDatahubPeripheral();
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const tz = useDisplayTz();

  const {
    register,
    handleSubmit,
    reset,
    watch,
    setValue,
    formState: { errors },
  } = useForm<DatahubFormValues>({
    resolver: zodResolver(datahubSchema),
    defaultValues: datahubToFormDefaults(peripheral),
  });

  const protocol = watch("kafka_security_protocol");
  const mechanism = watch("kafka_sasl_mechanism");
  const showMechanism = isSaslProtocol(protocol);
  const showCredentials = showMechanism && isCredentialMechanism(mechanism);
  const showAwsIam = showMechanism && mechanism === "AWS_MSK_IAM";

  /**
   * Clear the fields the new mechanism does not accept.
   *
   * The API rejects — rather than ignores — a credential under `AWS_MSK_IAM` and
   * a region under any other mechanism, so a value left behind by an earlier
   * selection would be submitted and rejected.
   */
  function applyMechanism(next: KafkaSaslMechanism | "") {
    setValue("kafka_sasl_mechanism", next, { shouldDirty: true, shouldValidate: true });
    if (!isCredentialMechanism(next)) {
      setValue("kafka_sasl_username", "", { shouldDirty: true });
      setValue("kafka_sasl_password", "", { shouldDirty: true });
    }
    if (next !== "AWS_MSK_IAM") {
      setValue("kafka_aws_region", "", { shouldDirty: true });
    }
  }

  function handleProtocolChange(next: KafkaSecurityProtocol) {
    setValue("kafka_security_protocol", next, { shouldDirty: true, shouldValidate: true });
    const offered = mechanismOptionsFor(next);
    const kept = offered.includes(mechanism as KafkaSaslMechanism) ? mechanism : "";
    applyMechanism(kept);
  }

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
        <div className="flex flex-col items-end gap-2">
          {/*
            Two independent planes, two badges. Each is keyed by its plane so a
            status change on one can never be reconciled into the other's node,
            and each reads its own field — nothing is derived from the pair.
          */}
          <div className="flex flex-wrap items-start justify-end gap-x-6 gap-y-2">
            <PeripheralHealthBadge
              key={EVENT_STREAM_PLANE.idPrefix}
              plane={EVENT_STREAM_PLANE}
              health={peripheral.health}
            />
            <PeripheralHealthBadge
              key={METADATA_API_PLANE.idPrefix}
              plane={METADATA_API_PLANE}
              health={peripheral.api_health}
            />
          </div>
          {savedAt && (
            <p className="text-xs text-muted-foreground">
              Saved · updated {formatDateTime(savedAt, tz)}
            </p>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <form id={DATAHUB_FORM_ID} onSubmit={handleSubmit(onSubmit)}>
          <FormGrid>
            <Field
              label="GMS URL"
              htmlFor="datahub_gms_url"
              description="DataHub GMS (metadata service) base URL used for REST + GraphQL calls. Must be a plain http(s) URL with no embedded credentials — authenticate with the token below, since a URL-borne credential can end up quoted in a stored transport error."
              error={errors.gms_url?.message}
            >
              <Input id="datahub_gms_url" {...register("gms_url")} />
            </Field>
            <Field
              label="Frontend URL (DataHub UI)"
              htmlFor="datahub_frontend_url"
              description="Browser-facing DataHub UI base URL — where users land, not the GMS URL above. These routinely differ in host, port, and scheme (e.g. GMS on an internal http://…:8080 endpoint, the UI on a public https:// hostname). Serves the header DataHub icon and dataset deep-links; leave blank to hide them."
              error={errors.frontend_url?.message}
            >
              <Input
                id="datahub_frontend_url"
                placeholder="https://datahub.example.com"
                {...register("frontend_url")}
              />
            </Field>
            <Field
              label="Kafka brokers"
              htmlFor="datahub_kafka_brokers"
              description={
                showAwsIam
                  ? "Bootstrap servers for the DataHub MCP/MCE Kafka topics (host:port, comma-separated). Under AWS_MSK_IAM every host must be an MSK broker (<broker>.kafka[-serverless].<region>.amazonaws.com) and all must share one region — the pod's IAM identity must not be redirected to any other host."
                  : "Bootstrap servers for the DataHub MCP/MCE Kafka topics (host:port, comma-separated)."
              }
              error={errors.kafka_brokers?.message}
            >
              <Input id="datahub_kafka_brokers" {...register("kafka_brokers")} />
            </Field>
            <Field
              label="Security protocol"
              htmlFor="datahub_kafka_security_protocol"
              description="How the event consumer connects to the brokers. PLAINTEXT — the default — needs nothing further."
              error={errors.kafka_security_protocol?.message}
            >
              <Select
                value={protocol}
                onValueChange={(v) => handleProtocolChange(v as KafkaSecurityProtocol)}
              >
                <SelectTrigger id="datahub_kafka_security_protocol">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {KAFKA_SECURITY_PROTOCOLS.map((p) => (
                    <SelectItem key={p} value={p}>
                      {p}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            {showMechanism && (
              // Keyed by protocol so switching protocols mounts a fresh select
              // instead of reusing the previous one's node.
              <Field
                key={`sasl-mechanism-${protocol}`}
                label="SASL mechanism"
                htmlFor="datahub_kafka_sasl_mechanism"
                description="AWS_MSK_IAM is offered only under SASL_SSL, the sole protocol it is accepted with."
                error={errors.kafka_sasl_mechanism?.message}
              >
                <Select
                  value={mechanism}
                  onValueChange={(v) => applyMechanism(v as KafkaSaslMechanism)}
                >
                  <SelectTrigger id="datahub_kafka_sasl_mechanism">
                    <SelectValue placeholder="Select a mechanism" />
                  </SelectTrigger>
                  <SelectContent>
                    {mechanismOptionsFor(protocol).map((m) => (
                      <SelectItem key={m} value={m}>
                        {m}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            )}
            {showCredentials && (
              // Keyed by mechanism so a typed credential cannot be carried into a
              // different mechanism's field by node reuse.
              <Field
                key={`sasl-username-${mechanism}`}
                label="SASL username"
                htmlFor="datahub_kafka_sasl_username"
                description={`Kafka user the consumer authenticates as with ${mechanism}.`}
                error={errors.kafka_sasl_username?.message}
              >
                <Input id="datahub_kafka_sasl_username" {...register("kafka_sasl_username")} />
              </Field>
            )}
            {showCredentials && (
              <Field
                key={`sasl-password-${mechanism}`}
                label="SASL password"
                htmlFor="datahub_kafka_sasl_password"
                description="Written to separated secure storage (currently a K8s Secret), never stored in the database. Leave blank to keep current."
                error={errors.kafka_sasl_password?.message}
              >
                <PasswordInput
                  id="datahub_kafka_sasl_password"
                  autoComplete="off"
                  {...register("kafka_sasl_password")}
                />
              </Field>
            )}
            {showAwsIam && (
              <Field
                key="sasl-aws-region"
                label="AWS region"
                htmlFor="datahub_kafka_aws_region"
                description="Region used to sign the MSK IAM token. Must match the region encoded in the broker hostnames. Leave blank to derive it from them."
                error={errors.kafka_aws_region?.message}
              >
                <Input
                  id="datahub_kafka_aws_region"
                  placeholder="ap-northeast-2"
                  {...register("kafka_aws_region")}
                />
              </Field>
            )}
            {showAwsIam && (
              <p
                id="datahub_kafka_aws_msk_iam_note"
                className="flex gap-2 rounded-md border border-info/40 bg-info/10 p-3 text-xs text-foreground sm:col-span-2"
              >
                <Info className="mt-0.5 h-4 w-4 shrink-0 text-info" aria-hidden="true" />
                <span>
                  AWS_MSK_IAM takes no username or password — the consumer authenticates with
                  its pod IAM role, and any stored SASL password is cleared on save. This form
                  alone is not sufficient: the role must be attached at deploy time via IRSA
                  (chart values <code>event-consumer.serviceAccount</code>).
                </span>
              </p>
            )}
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

"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Field } from "@/components/forms/field";
import { ErrorState } from "@/components/ui/error-state";
import { RecipeYamlEditor } from "@/components/ingestion/recipe-yaml-editor";
import { SecretRefHelper } from "@/components/ingestion/secret-ref-helper";
import { validateSourceBody } from "@/components/ingestion/recipe-yaml";
import {
  useCreateIngestionSource,
  useIngestionSecrets,
} from "@/lib/api/ingestion";
import { useMe } from "@/lib/auth/use-me";
import { modeDescription } from "@/lib/ingestion-mode-variant";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import type { IngestionMode, IngestionSourceBody } from "@/types/ingestion";

// Creatable modes only — DATAHUB_MANAGED is synced from DataHub.
const CREATABLE_MODES: { value: IngestionMode; label: string }[] = [
  { value: "ACTIVE_CUSTOM_MANAGED", label: "Active (custom managed)" },
  { value: "PASSIVE", label: "Passive" },
];

// Recipe-only templates: the page owns mode/name/schedule; the editor manages
// the recipe object alone (validated with `recipeOnly`).
const ACTIVE_TEMPLATE = `source:
  type: postgres
  config:
    host_port: pg.example:5432
    username: spoke_reader
    password: \${dummy-data-pg__password}
    schema_pattern:
      allow:
        - "^catalog$"
    env: DEV
`;

const PASSIVE_TEMPLATE = `source:
  type: s3
  config:
    path_specs:
      - include: "s3://bucket/path/*"
`;

export default function CreateIngestionSourcePage() {
  const router = useRouter();
  const { canWrite } = useMe();

  const [mode, setMode] = useState<IngestionMode>("ACTIVE_CUSTOM_MANAGED");
  const [name, setName] = useState("");
  const [schedule, setSchedule] = useState<"manual" | "hourly" | "daily" | "weekly">(
    "daily",
  );
  const [composeError, setComposeError] = useState<string | undefined>();

  const recipeTemplate = mode === "PASSIVE" ? PASSIVE_TEMPLATE : ACTIVE_TEMPLATE;
  // The YAML editor manages its own recipe text; we re-key it on mode change so
  // the per-mode template is loaded fresh.
  const [recipeKey, setRecipeKey] = useState(0);

  const create = useCreateIngestionSource();
  const secrets = useIngestionSecrets(canWrite);

  const secretsUnavailable =
    secrets.error instanceof ApiError && secrets.error.status === 503;

  const editorValue = useMemo(() => recipeTemplate, [recipeTemplate]);

  if (!canWrite) {
    return (
      <div className="space-y-2">
        <ErrorState message="You need the Editor role to create an ingestion source." />
        <Button variant="outline" size="sm" asChild>
          <Link href="/ingestion">Back to list</Link>
        </Button>
      </div>
    );
  }

  const serverError =
    composeError ??
    (create.error instanceof ApiError
      ? `${create.error.error_code}: ${create.error.message}`
      : create.error?.message);

  // The editor validates only the recipe shape (recipeOnly); the page owns
  // mode/name/schedule, composes the full body, and runs the complete
  // validateSourceBody before POST so name/schedule constraints are enforced.
  function handleRecipeSave(recipe: Record<string, unknown>) {
    const tier =
      mode === "PASSIVE" || schedule === "manual"
        ? null
        : schedule === "hourly"
          ? "0 * * * *"
          : schedule === "weekly"
            ? "0 0 * * 0"
            : "0 0 * * *";

    const candidate: IngestionSourceBody = {
      mode,
      name,
      schedule: tier,
      recipe,
    };

    const validated = validateSourceBody(candidate, { creatableOnly: true });
    if (!validated.ok || !validated.body) {
      setComposeError(validated.error);
      return;
    }
    setComposeError(undefined);

    create.mutate(validated.body, {
      onSuccess: (created) => {
        toast({ title: "Source created", description: created.name });
        router.push(`/ingestion/sources/${encodeURIComponent(created.id)}`);
      },
    });
  }

  return (
    <div className="max-w-3xl space-y-6">
      <div className="flex flex-wrap items-center gap-3">
        <Link
          href="/ingestion"
          className="text-muted-foreground hover:text-foreground"
          aria-label="Back to ingestion list"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight">
          Create ingestion source
        </h1>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="mode" htmlFor="create-mode" hint={modeDescription(mode)}>
          <Select
            value={mode}
            onValueChange={(v) => {
              setMode(v as IngestionMode);
              setRecipeKey((k) => k + 1);
            }}
          >
            <SelectTrigger id="create-mode">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CREATABLE_MODES.map((m) => (
                <SelectItem key={m.value} value={m.value}>
                  {m.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>

        <Field label="name" htmlFor="create-name" required>
          <Input
            id="create-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="prod postgres catalog schema"
            maxLength={512}
          />
        </Field>

        {mode === "ACTIVE_CUSTOM_MANAGED" && (
          <Field
            label="schedule"
            htmlFor="create-schedule"
            hint="ACTIVE sources run on one of three tiers, or manual-only."
          >
            <Select
              value={schedule}
              onValueChange={(v) =>
                setSchedule(v as "manual" | "hourly" | "daily" | "weekly")
              }
            >
              <SelectTrigger id="create-schedule">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="manual">manual (no schedule)</SelectItem>
                <SelectItem value="hourly">hourly</SelectItem>
                <SelectItem value="daily">daily</SelectItem>
                <SelectItem value="weekly">weekly</SelectItem>
              </SelectContent>
            </Select>
          </Field>
        )}
      </div>

      {/* Secret references helper — list + read-only authoring guide. Shown for
          ACTIVE_CUSTOM_MANAGED, whose recipes carry ${name__key} refs. */}
      {mode === "ACTIVE_CUSTOM_MANAGED" && (
        <SecretRefHelper
          secrets={secrets.data?.secrets}
          unavailable={secretsUnavailable}
        />
      )}

      {/* Recipe editor */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">recipe</h2>
        <RecipeYamlEditor
          key={recipeKey}
          value={editorValue}
          editing
          onRecipeSave={handleRecipeSave}
          isSaving={create.isPending}
          serverError={serverError}
          validateOptions={{ recipeOnly: true }}
        />
      </section>
    </div>
  );
}

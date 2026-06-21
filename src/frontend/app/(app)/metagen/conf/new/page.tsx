"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { MetagenConfForm } from "@/components/metagen/conf-form";
import { useCreateMetagenConf } from "@/lib/api/metagen";
import { useMe } from "@/lib/auth/use-me";
import { ApiError } from "@/lib/api/client";
import { useToast } from "@/components/ui/use-toast";
import type { DatasetFilter } from "@/types/governance";
import type { MetagenConfPutBody } from "@/types/metagen";

const CONF_FORM_ID = "metagen-conf-form";

export default function CreateMetagenConfPage() {
  const router = useRouter();
  const { canWrite } = useMe();
  const { toast } = useToast();

  const [datasetFilter, setDatasetFilter] = useState<DatasetFilter>({});
  const create = useCreateMetagenConf();

  if (!canWrite) {
    return (
      <div className="space-y-2">
        <ErrorState message="You need the Editor role to create a conf." />
        <Button variant="outline" size="sm" asChild>
          <Link href="/metagen/conf">Back to list</Link>
        </Button>
      </div>
    );
  }

  const serverError =
    create.error instanceof ApiError
      ? `${create.error.error_code}: ${create.error.message}`
      : create.error?.message;

  function handleSubmit(body: MetagenConfPutBody) {
    create.mutate(body, {
      onSuccess: (created) => {
        toast({ title: "Conf created", description: created.name });
        router.push(`/metagen/conf/${encodeURIComponent(created.id)}`);
      },
    });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link
            href="/metagen/conf"
            className="text-muted-foreground hover:text-foreground"
            aria-label="Back to conf list"
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight">Create conf</h1>
        </div>
        <Button type="submit" form={CONF_FORM_ID} disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create conf"}
        </Button>
      </div>

      <section className="rounded-lg border p-5">
        <MetagenConfForm
          initialValues={null}
          datasetFilter={datasetFilter}
          onDatasetFilterChange={setDatasetFilter}
          onSubmit={handleSubmit}
          serverError={serverError}
          formId={CONF_FORM_ID}
        />
      </section>
    </div>
  );
}

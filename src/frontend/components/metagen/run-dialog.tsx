"use client";

/**
 * RunDialog — trigger a global MetaGen run with optional dataset URN filter and dry-run toggle.
 *
 * The dataset_urns override is a newline-separated textarea parsed on submit
 * (lib/urn-list.ts): the box holds the raw text the user typed — one URN per
 * line, each line edge-trimmed, blank lines dropped. Commas are not separators;
 * a dataset URN contains them by construction.
 */

import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Field } from "@/components/forms/field";
import { Textarea } from "@/components/ui/textarea";
import { splitList } from "@/lib/urn-list";
import type { MetagenRunBody } from "@/types/metagen";

interface RunDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRun: (body: MetagenRunBody) => void;
  isRunning: boolean;
}

export function RunDialog({ open, onOpenChange, onRun, isRunning }: RunDialogProps) {
  const [datasetUrnsRaw, setDatasetUrnsRaw] = useState("");
  const [dryRun, setDryRun] = useState(false);

  function handleRun() {
    const urns = splitList(datasetUrnsRaw);
    onRun({
      dataset_urns: urns.length > 0 ? urns : null,
      dry_run: dryRun,
    });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle>Run MetaGen</DialogTitle>
          <DialogDescription>
            Trigger the metadata generation inference pipeline. Leave dataset URNs empty
            to run against all datasets in scope.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <Field
            label="dataset_urns (optional)"
            htmlFor="run-dataset-urns"
            hint="One URN per line — blank lines dropped, each line edge-trimmed. Empty = all in-scope datasets."
          >
            <Textarea
              id="run-dataset-urns"
              rows={4}
              value={datasetUrnsRaw}
              onChange={(e) => setDatasetUrnsRaw(e.target.value)}
              placeholder="urn:li:dataset:(...)"
              className="font-mono text-xs"
              disabled={isRunning}
            />
          </Field>

          <div className="flex items-center gap-2">
            <Checkbox
              id="run-dry-run"
              checked={dryRun}
              onCheckedChange={(v) => setDryRun(!!v)}
              disabled={isRunning}
            />
            <label htmlFor="run-dry-run" className="cursor-pointer text-sm">
              dry_run — simulate without writing to DataHub
            </label>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isRunning}>
            Cancel
          </Button>
          <Button onClick={handleRun} disabled={isRunning}>
            {isRunning ? "Running…" : dryRun ? "Dry Run" : "Run"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

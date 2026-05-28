"use client";

/**
 * RunDialog — triggers a manual ontogen inference run.
 * Optional one-shot Markdown prompt and dry-run toggle.
 * POST /spoke/ontogen/method/run
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
import { Textarea } from "@/components/ui/textarea";
import { Field } from "@/components/forms/field";

interface RunDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRun: (params: { promptMd?: string; dry_run: boolean }) => void;
  isRunning: boolean;
}

export function RunDialog({ open, onOpenChange, onRun, isRunning }: RunDialogProps) {
  const [promptMd, setPromptMd] = useState("");
  const [dryRun, setDryRun] = useState(false);

  function handleRun() {
    onRun({ promptMd: promptMd.trim() || undefined, dry_run: dryRun });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle>Run ontology inference</DialogTitle>
          <DialogDescription>
            Trigger a manual re-inference run. Optionally provide a one-shot prompt that
            overrides the default for this run only (not stored).
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <Field
            label="one-shot prompt"
            htmlFor="run-prompt"
            hint="Overrides conf.default_run_prompt for this run only. Leave blank to use the stored default."
          >
            <Textarea
              id="run-prompt"
              rows={6}
              placeholder="# One-shot prompt&#10;&#10;Describe the ontology inference context for this run…"
              value={promptMd}
              onChange={(e) => setPromptMd(e.target.value)}
              disabled={isRunning}
              className="font-mono text-xs"
            />
          </Field>

          <div className="flex items-center gap-2">
            <Checkbox
              id="run-dry-run"
              checked={dryRun}
              onCheckedChange={(v) => setDryRun(!!v)}
              disabled={isRunning}
            />
            <label htmlFor="run-dry-run" className="text-sm text-muted-foreground">
              Dry run — evaluate without persisting results
            </label>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isRunning}>
            Cancel
          </Button>
          <Button onClick={handleRun} disabled={isRunning}>
            {isRunning ? "Running…" : dryRun ? "Dry run" : "Run"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

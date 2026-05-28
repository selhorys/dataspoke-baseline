"use client";

/**
 * SeedEditor — view/create/edit a single Markdown seed.
 *
 * Markdown is displayed as plain preformatted text (no HTML rendering) to
 * avoid XSS risks in the baseline. A future design-polish pass may replace
 * the <pre> with a sanitizing Markdown renderer.
 */

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

interface SeedEditorProps {
  /** Initial Markdown body (null for new-seed creation). */
  initialBody: string | null;
  isLoading?: boolean;
  onSave: (body: string) => void;
  onCancel?: () => void;
  isSaving?: boolean;
  disabled?: boolean;
  /** When true, renders in view mode (non-editable) with an Edit button. */
  readOnly?: boolean;
  onEditRequest?: () => void;
}

export function SeedEditor({
  initialBody,
  isLoading = false,
  onSave,
  onCancel,
  isSaving = false,
  disabled = false,
  readOnly = false,
  onEditRequest,
}: SeedEditorProps) {
  const [body, setBody] = useState(initialBody ?? "");

  useEffect(() => {
    setBody(initialBody ?? "");
  }, [initialBody]);

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  if (readOnly) {
    return (
      <div className="space-y-2">
        {onEditRequest && (
          <div className="flex justify-end">
            <Button size="sm" variant="outline" onClick={onEditRequest} disabled={disabled}>
              Edit
            </Button>
          </div>
        )}
        <pre className="overflow-auto rounded-md border bg-muted/40 p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap">
          {body || <span className="text-muted-foreground">(empty)</span>}
        </pre>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <Textarea
        rows={16}
        className="font-mono text-xs"
        placeholder="# Ontology seed&#10;&#10;Describe the ontology inference context…"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        disabled={isSaving || disabled}
      />
      <div className="flex gap-2">
        <Button onClick={() => onSave(body)} disabled={isSaving || disabled || !body.trim()}>
          {isSaving ? "Saving…" : "Save seed"}
        </Button>
        {onCancel && (
          <Button variant="outline" onClick={onCancel} disabled={isSaving}>
            Cancel
          </Button>
        )}
      </div>
    </div>
  );
}

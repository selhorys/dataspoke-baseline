"use client";

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Plus, Copy, Check } from "lucide-react";
import { useApiTokens, useCreateApiToken, useDeleteApiToken } from "@/lib/api/auth";
import type { ApiTokenMintResponse } from "@/lib/api/types";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/forms/field";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";

const mintSchema = z.object({
  name: z.string().min(1, "Name is required").max(128, "Name is too long"),
});

type MintFormValues = z.infer<typeof mintSchema>;

const EXPIRY_OPTIONS = [
  { value: "never", label: "never", days: null },
  { value: "30d", label: "30 days", days: 30 },
  { value: "90d", label: "90 days", days: 90 },
  { value: "1y", label: "1 year", days: 365 },
] as const;

type ExpiryValue = (typeof EXPIRY_OPTIONS)[number]["value"];

/** Compute an ISO-8601 UTC datetime `now + days`, or null for a non-expiring token. */
function computeExpiresAt(value: ExpiryValue): string | null {
  const days = EXPIRY_OPTIONS.find((o) => o.value === value)?.days ?? null;
  if (days === null) return null;
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString();
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString();
}

export default function ProfileTokensPage() {
  const { data, isLoading } = useApiTokens();
  const { mutateAsync: createToken, isPending: creating } = useCreateApiToken();
  const { mutateAsync: deleteToken, isPending: deleting } = useDeleteApiToken();

  const [mintOpen, setMintOpen] = useState(false);
  const [expiry, setExpiry] = useState<ExpiryValue>("never");
  const [mintedToken, setMintedToken] = useState<ApiTokenMintResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokeId, setRevokeId] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    reset: resetMintForm,
    formState: { errors: mintErrors },
  } = useForm<MintFormValues>({ resolver: zodResolver(mintSchema) });

  async function onMint(values: MintFormValues) {
    try {
      const result = await createToken({
        name: values.name,
        expires_at: computeExpiresAt(expiry),
      });
      setMintedToken(result);
      setMintOpen(false);
      resetMintForm();
      setExpiry("never");
    } catch (err) {
      if (err instanceof ApiError) {
        toast({ variant: "destructive", title: "Token creation failed", description: err.message });
      } else {
        toast({ variant: "destructive", title: "Token creation failed", description: "An unexpected error occurred." });
      }
    }
  }

  async function onRevoke(id: string) {
    try {
      await deleteToken({ id });
      setRevokeId(null);
      toast({ title: "Token revoked." });
    } catch (err) {
      if (err instanceof ApiError) {
        toast({ variant: "destructive", title: "Revoke failed", description: err.message });
      } else {
        toast({ variant: "destructive", title: "Revoke failed", description: "An unexpected error occurred." });
      }
    }
  }

  async function copyToken(token: string) {
    await navigator.clipboard.writeText(token);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">API Tokens</h1>
        <Button size="sm" onClick={() => setMintOpen(true)}>
          <Plus className="h-4 w-4" />
          New token
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : data?.tokens.length === 0 ? (
        <p className="text-sm text-muted-foreground">No API tokens yet. Create one to get started.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Last used</TableHead>
              <TableHead>Expires</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {data?.tokens.map((t) => (
              <TableRow key={t.id}>
                <TableCell className="font-medium">{t.name}</TableCell>
                <TableCell>{t.role_snapshot}</TableCell>
                <TableCell>{formatDate(t.created_at)}</TableCell>
                <TableCell>{formatDate(t.last_used_at)}</TableCell>
                <TableCell>{formatDate(t.expires_at)}</TableCell>
                <TableCell>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => setRevokeId(t.id)}
                  >
                    Revoke
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {/* Mint dialog */}
      <Dialog open={mintOpen} onOpenChange={setMintOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>New API token</DialogTitle>
            <DialogDescription>
              Give this token a descriptive name so you can identify it later.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={handleSubmit(onMint)} className="space-y-4">
            <Field label="Name" htmlFor="token-name" error={mintErrors.name?.message} required>
              <Input
                id="token-name"
                placeholder="e.g. ci-jenkins"
                {...register("name")}
              />
            </Field>
            <Field label="Expiry" htmlFor="token-expiry">
              <Select value={expiry} onValueChange={(v) => setExpiry(v as ExpiryValue)}>
                <SelectTrigger id="token-expiry">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {EXPIRY_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>
                      {o.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setMintOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={creating}>
                {creating ? "Creating..." : "Create"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Token reveal dialog (one-shot) */}
      <Dialog
        open={!!mintedToken}
        onOpenChange={(open) => {
          if (!open) {
            setMintedToken(null);
            setCopied(false);
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Your new token</DialogTitle>
            <DialogDescription>
              Copy this token now. It will not be shown again after you close this dialog.
            </DialogDescription>
          </DialogHeader>
          <div className="flex items-center gap-2 rounded-md border bg-muted px-3 py-2">
            <code className="flex-1 break-all text-sm">{mintedToken?.token}</code>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              onClick={() => mintedToken && copyToken(mintedToken.token)}
              aria-label="Copy token"
            >
              {copied ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
            </Button>
          </div>
          <DialogFooter>
            <Button
              type="button"
              onClick={() => {
                setMintedToken(null);
                setCopied(false);
              }}
            >
              {copied ? "Done" : "Close"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Revoke confirm dialog */}
      <ConfirmDialog
        open={!!revokeId}
        onOpenChange={(open) => !open && setRevokeId(null)}
        title="Revoke token"
        description="This will permanently revoke the API token. Any systems using it will lose access immediately."
        confirmLabel="Revoke"
        onConfirm={() => revokeId && onRevoke(revokeId)}
        loading={deleting}
      />
    </div>
  );
}

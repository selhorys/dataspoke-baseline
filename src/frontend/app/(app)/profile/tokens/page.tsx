"use client";

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Plus, Copy, Check } from "lucide-react";
import { useApiTokens, useCreateApiToken, useDeleteApiToken } from "@/lib/api/auth";
import { useAdminApiTokens, useDeleteAdminUserToken } from "@/lib/api/admin";
import { useMe } from "@/lib/auth/use-me";
import type {
  AdminApiTokenItem,
  AdminApiTokenListResponse,
  ApiTokenListResponse,
  ApiTokenMintResponse,
} from "@/lib/api/types";
import { ApiError } from "@/lib/api/client";
import { copyToClipboard } from "@/lib/clipboard";
import { tokenStatus } from "@/lib/token-status";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { QueryErrorState } from "@/components/query-error-state";
import { TokenStatusBadge } from "@/components/token-status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate } from "@/lib/format-time";
import type { TzMode } from "@/lib/range";
import { useDisplayTz } from "@/lib/preferences/timezone";

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

/** Which tokens the page lists: the caller's own, or every user's. */
type Scope = "mine" | "all";

/** Compute an ISO-8601 UTC datetime `now + days`, or null for a non-expiring token. */
function computeExpiresAt(value: ExpiryValue): string | null {
  const days = EXPIRY_OPTIONS.find((o) => o.value === value)?.days ?? null;
  if (days === null) return null;
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString();
}

// ── My tokens ─────────────────────────────────────────────────────────────────

interface MyTokensTableProps {
  data: ApiTokenListResponse | undefined;
  isLoading: boolean;
  error: unknown;
  tz: TzMode;
  onRevoke: (id: string) => void;
}

function MyTokensTable({ data, isLoading, error, tz, onRevoke }: MyTokensTableProps) {
  if (error) {
    return <QueryErrorState error={error} context="Failed to load API tokens" />;
  }
  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
      </div>
    );
  }
  if ((data?.tokens.length ?? 0) === 0) {
    return (
      <p className="text-sm text-muted-foreground">No API tokens yet. Create one to get started.</p>
    );
  }
  return (
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
            <TableCell>{formatDate(t.created_at, tz)}</TableCell>
            <TableCell>{formatDate(t.last_used_at, tz)}</TableCell>
            <TableCell>{formatDate(t.expires_at, tz)}</TableCell>
            <TableCell>
              <Button variant="destructive" size="sm" onClick={() => onRevoke(t.id)}>
                Revoke
              </Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// ── All tokens (Admin) ────────────────────────────────────────────────────────

interface AllTokensTableProps {
  data: AdminApiTokenListResponse | undefined;
  isLoading: boolean;
  error: unknown;
  showRevoked: boolean;
  tz: TzMode;
  offset: number;
  limit: number;
  onOffset: (offset: number) => void;
  onLimit: (limit: number) => void;
  onRevoke: (token: AdminApiTokenItem) => void;
}

function AllTokensTable({
  data,
  isLoading,
  error,
  showRevoked,
  tz,
  offset,
  limit,
  onOffset,
  onLimit,
  onRevoke,
}: AllTokensTableProps) {
  if (error) {
    return <QueryErrorState error={error} context="Failed to load the token inventory" />;
  }
  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
      </div>
    );
  }
  return (
    <div className="space-y-4">
      {(data?.tokens.length ?? 0) === 0 ? (
        <p className="text-sm text-muted-foreground">
          {showRevoked
            ? "No API tokens exist in this deployment."
            : "No active API tokens. Turn on “Show revoked” to see withdrawn ones."}
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Owner</TableHead>
              <TableHead>Name</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Last used</TableHead>
              <TableHead>Expires</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {data?.tokens.map((t) => (
              <TableRow key={t.id}>
                <TableCell className="font-medium">{t.user_email}</TableCell>
                <TableCell>{t.name}</TableCell>
                <TableCell>{t.role_snapshot}</TableCell>
                <TableCell>
                  <TokenStatusBadge token={t} tz={tz} />
                </TableCell>
                <TableCell>{formatDate(t.created_at, tz)}</TableCell>
                <TableCell>{formatDate(t.last_used_at, tz)}</TableCell>
                <TableCell>{formatDate(t.expires_at, tz)}</TableCell>
                <TableCell>
                  {/* A revoked token grants nothing, so there is nothing left
                      to withdraw. An expired one is still revocable — expiry is
                      a clock, revocation is a decision. */}
                  {tokenStatus(t) !== "revoked" && (
                    <Button variant="destructive" size="sm" onClick={() => onRevoke(t)}>
                      Revoke
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <Pagination
        offset={offset}
        limit={limit}
        total={data?.total_count ?? 0}
        onOffset={onOffset}
        onLimit={onLimit}
      />
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function ProfileTokensPage() {
  const tz = useDisplayTz();
  const { me, isAdmin } = useMe();
  const { data, isLoading, error } = useApiTokens();
  const { mutateAsync: createToken, isPending: creating } = useCreateApiToken();
  const { mutateAsync: deleteToken, isPending: deleting } = useDeleteApiToken();

  const [scope, setScope] = useState<Scope>("mine");
  const [showRevoked, setShowRevoked] = useState(false);
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [mintOpen, setMintOpen] = useState(false);
  const [expiry, setExpiry] = useState<ExpiryValue>("never");
  const [mintedToken, setMintedToken] = useState<ApiTokenMintResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [revokeId, setRevokeId] = useState<string | null>(null);
  const [adminRevokeTarget, setAdminRevokeTarget] = useState<AdminApiTokenItem | null>(null);

  // The inventory request is held back until the client believes the session is
  // Admin — `isAdmin` is derived from the same object returned as `me`, so it
  // already implies an identity. This is not an authorization decision;
  // `require_admin` on the route is. It only keeps a session that would 403
  // from asking.
  const allScope = isAdmin && scope === "all";
  const {
    data: adminData,
    isLoading: adminLoading,
    error: adminError,
  } = useAdminApiTokens({
    callerId: me?.id,
    offset,
    limit,
    includeRevoked: showRevoked,
    enabled: allScope,
  });
  const { mutateAsync: revokeAdminToken, isPending: adminRevoking } = useDeleteAdminUserToken();

  // A revoke can empty the last page: `total_count` shrinks under a fixed
  // `offset`, leaving Pagination with no page to travel to and the table
  // claiming the inventory is empty. Pull the window back to the last page that
  // still exists. Runs during render, so the corrected offset reaches the query
  // in the same commit.
  const adminTotal = adminData?.total_count;
  if (allScope && adminTotal !== undefined) {
    const maxOffset = Math.max(0, (Math.ceil(adminTotal / limit) - 1) * limit);
    if (offset > maxOffset) setOffset(maxOffset);
  }

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

  /**
   * Revoke from the All-tokens scope. Always the admin route, addressed by the
   * row's `user_id` — including for the caller's own token, so the confirm copy
   * says one thing.
   */
  async function onAdminRevoke(token: AdminApiTokenItem) {
    try {
      await revokeAdminToken({ userId: token.user_id, tokenId: token.id });
      setAdminRevokeTarget(null);
      toast({ title: "Token revoked." });
    } catch (err) {
      if (err instanceof ApiError) {
        toast({ variant: "destructive", title: "Revoke failed", description: err.message });
      } else {
        toast({ variant: "destructive", title: "Revoke failed", description: "An unexpected error occurred." });
      }
    }
  }

  /** Never rejects: a failed copy is reported to the user, not thrown. */
  async function onCopyToken(token: string) {
    let ok = false;
    try {
      ok = await copyToClipboard(token);
    } catch {
      ok = false;
    }
    if (!ok) {
      toast({
        variant: "destructive",
        title: "Copy failed",
        description: "Select the token above and copy it manually before closing this dialog.",
      });
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  const newTokenButton = (
    <Button size="sm" onClick={() => setMintOpen(true)}>
      <Plus className="h-4 w-4" />
      New token
    </Button>
  );

  const myTokensTable = (
    <MyTokensTable
      data={data}
      isLoading={isLoading}
      error={error}
      tz={tz}
      onRevoke={setRevokeId}
    />
  );

  return (
    <div>
      {/* The scope control exists for Admins only. Every other role gets the
          page it had before it existed — no tablist, and therefore no tabpanel
          to label. */}
      {isAdmin ? (
        <Tabs value={scope} onValueChange={(v) => setScope(v as Scope)}>
          <div className="mb-6 flex items-center justify-between gap-4">
            <div className="flex items-center gap-4">
              <h1 className="text-2xl font-semibold tracking-tight">API Tokens</h1>
              <TabsList>
                <TabsTrigger value="mine">My tokens</TabsTrigger>
                <TabsTrigger value="all">All tokens</TabsTrigger>
              </TabsList>
            </div>
            {scope === "all" ? (
              <div className="flex items-center gap-2">
                <Checkbox
                  id="tokens-show-revoked"
                  checked={showRevoked}
                  onCheckedChange={(v) => {
                    setShowRevoked(!!v);
                    // The result set changes, so the current page number no
                    // longer means anything.
                    setOffset(0);
                  }}
                />
                <label htmlFor="tokens-show-revoked" className="cursor-pointer text-sm">
                  Show revoked
                </label>
              </div>
            ) : (
              /* Minting is self-only, so the control is absent from a table of
                 other users' tokens. */
              newTokenButton
            )}
          </div>

          <TabsContent value="mine">{myTokensTable}</TabsContent>

          <TabsContent value="all">
            <AllTokensTable
              data={adminData}
              isLoading={adminLoading}
              error={adminError}
              showRevoked={showRevoked}
              tz={tz}
              offset={offset}
              limit={limit}
              onOffset={setOffset}
              onLimit={setLimit}
              onRevoke={setAdminRevokeTarget}
            />
          </TabsContent>
        </Tabs>
      ) : (
        <>
          <div className="mb-6 flex items-center justify-between">
            <h1 className="text-2xl font-semibold tracking-tight">API Tokens</h1>
            {newTokenButton}
          </div>
          {myTokensTable}
        </>
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
              onClick={() => {
                if (mintedToken) void onCopyToken(mintedToken.token);
              }}
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

      {/* Revoke confirm dialog — own token */}
      <ConfirmDialog
        open={!!revokeId}
        onOpenChange={(open) => !open && setRevokeId(null)}
        title="Revoke token"
        description="This will permanently revoke the API token. Any systems using it will lose access immediately."
        confirmLabel="Revoke"
        onConfirm={() => revokeId && onRevoke(revokeId)}
        loading={deleting}
      />

      {/* Revoke confirm dialog — All-tokens scope */}
      <ConfirmDialog
        open={!!adminRevokeTarget}
        onOpenChange={(open) => !open && setAdminRevokeTarget(null)}
        title="Revoke token"
        description={
          adminRevokeTarget
            ? `Permanently revoke “${adminRevokeTarget.name}”, held by ${adminRevokeTarget.user_email}? Any systems using it will lose access immediately.`
            : "Permanently revoke this token?"
        }
        confirmLabel="Revoke"
        onConfirm={() => adminRevokeTarget && onAdminRevoke(adminRevokeTarget)}
        loading={adminRevoking}
      />
    </div>
  );
}

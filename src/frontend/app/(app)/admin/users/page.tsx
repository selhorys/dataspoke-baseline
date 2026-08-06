"use client";

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { MoreHorizontal, Pencil } from "lucide-react";
import { useMe } from "@/lib/auth/use-me";
import {
  useAdminUsers,
  useUpdateUserName,
  useUpdateUserRole,
  useDeleteUser,
  useUnlinkUserGoogle,
  useAdminUserTokens,
  useDeleteAdminUserToken,
} from "@/lib/api/admin";
import type { AdminUser, UserRole } from "@/lib/api/types";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { QueryErrorState } from "@/components/query-error-state";
import { TokenStatusBadge } from "@/components/token-status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate } from "@/lib/format-time";
import { tokenStatus } from "@/lib/token-status";
import { useDisplayTz } from "@/lib/preferences/timezone";

const roles: UserRole[] = ["Admin", "Editor", "Reader"];

const renameSchema = z.object({
  name: z.string().min(1, "Name is required").max(128, "Name is too long"),
});
type RenameValues = z.infer<typeof renameSchema>;

/**
 * Reports a failed write on this page. A `401` is skipped, following the same
 * rule as `lib/toast-api-error.ts`: the API client has already cleared the
 * session and the guard is redirecting to `/login`, so a failure toast would
 * land over the sign-in page. A superseded session epoch reaches this path as
 * an ordinary mid-session `401`.
 */
function toastWriteFailure(title: string, err: unknown): void {
  if (err instanceof ApiError) {
    if (err.status === 401) return;
    toast({ variant: "destructive", title, description: err.message });
    return;
  }
  toast({ variant: "destructive", title, description: "An unexpected error occurred." });
}

// ── Inline role select ────────────────────────────────────────────────────────

function RoleSelect({ user }: { user: AdminUser }) {
  const { mutateAsync: updateRole, isPending } = useUpdateUserRole();

  async function onChange(role: UserRole) {
    try {
      await updateRole({ id: user.id, role });
      toast({ title: `Role updated to ${role}.` });
    } catch (err) {
      toastWriteFailure("Role update failed", err);
    }
  }

  return (
    <Select value={user.role} onValueChange={(v) => onChange(v as UserRole)} disabled={isPending}>
      <SelectTrigger className="h-8 w-28 text-xs" disabled={isPending}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {roles.map((r) => (
          <SelectItem key={r} value={r}>
            {r}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

// ── Rename dialog ─────────────────────────────────────────────────────────────

function RenameDialog({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const { mutateAsync: updateName, isPending } = useUpdateUserName();

  const { register, handleSubmit, formState: { errors } } = useForm<RenameValues>({
    resolver: zodResolver(renameSchema),
    defaultValues: { name: user.name },
  });

  async function onSubmit(values: RenameValues) {
    try {
      await updateName({ id: user.id, name: values.name });
      toast({ title: "Name updated." });
      onClose();
    } catch (err) {
      toastWriteFailure("Update failed", err);
    }
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
      <div className="space-y-1.5">
        <Input id="rename-name" {...register("name")} />
        {errors.name && <p className="text-sm text-destructive">{errors.name.message}</p>}
      </div>
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" disabled={isPending}>
          {isPending ? "Saving..." : "Save"}
        </Button>
      </div>
    </form>
  );
}

// ── User tokens drawer ────────────────────────────────────────────────────────

function UserTokensDialog({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const [showRevoked, setShowRevoked] = useState(false);
  const { data, isLoading, error } = useAdminUserTokens(user.id, { includeRevoked: showRevoked });
  const { mutateAsync: revokeToken, isPending: revoking } = useDeleteAdminUserToken();
  const [confirmTokenId, setConfirmTokenId] = useState<string | null>(null);
  const tz = useDisplayTz();

  async function onRevoke(tokenId: string) {
    try {
      await revokeToken({ userId: user.id, tokenId });
      setConfirmTokenId(null);
      toast({ title: "Token revoked." });
    } catch (err) {
      toastWriteFailure("Revoke failed", err);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Checkbox
          id="user-tokens-show-revoked"
          checked={showRevoked}
          onCheckedChange={(v) => setShowRevoked(!!v)}
        />
        <label htmlFor="user-tokens-show-revoked" className="cursor-pointer text-sm">
          Show revoked
        </label>
      </div>
      {error ? (
        <QueryErrorState error={error} context="Failed to load tokens" />
      ) : isLoading ? (
        <Skeleton className="h-20 w-full" />
      ) : (data?.tokens.length ?? 0) === 0 ? (
        <p className="text-sm text-muted-foreground">
          {showRevoked ? "No tokens." : "No active tokens."}
        </p>
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Created</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {data?.tokens.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="font-medium">{t.name}</TableCell>
                  <TableCell>
                    <TokenStatusBadge token={t} tz={tz} />
                  </TableCell>
                  <TableCell>{formatDate(t.created_at, tz)}</TableCell>
                  <TableCell>
                    {/* A revoked token grants nothing, so there is nothing left
                        to withdraw. An expired one is still revocable — expiry
                        is a clock, revocation is a decision. */}
                    {tokenStatus(t) !== "revoked" && (
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() => setConfirmTokenId(t.id)}
                      >
                        Revoke
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {data && data.total_count > data.tokens.length && (
            <p className="text-sm text-muted-foreground">
              Showing {data.tokens.length} of {data.total_count} tokens.
            </p>
          )}
        </>
      )}
      <div className="flex justify-end">
        <Button variant="outline" onClick={onClose}>
          Close
        </Button>
      </div>
      <ConfirmDialog
        open={!!confirmTokenId}
        onOpenChange={(open) => !open && setConfirmTokenId(null)}
        title="Revoke token"
        description="This will permanently revoke the token. The user will lose access via this token immediately."
        confirmLabel="Revoke"
        onConfirm={() => confirmTokenId && onRevoke(confirmTokenId)}
        loading={revoking}
      />
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

type DialogState =
  | { kind: "rename"; user: AdminUser }
  | { kind: "tokens"; user: AdminUser }
  | { kind: "delete"; user: AdminUser }
  | { kind: "unlink-google"; user: AdminUser }
  | null;

export default function AdminUsersPage() {
  const { isAdmin, isLoading: meLoading } = useMe();
  const { data, isLoading } = useAdminUsers();
  const { mutateAsync: deleteUser, isPending: deleting } = useDeleteUser();
  const { mutateAsync: unlinkGoogle, isPending: unlinking } = useUnlinkUserGoogle();
  const [dialog, setDialog] = useState<DialogState>(null);
  const [search, setSearch] = useState("");
  const tz = useDisplayTz();

  async function onDeleteUser(user: AdminUser) {
    try {
      await deleteUser({ id: user.id });
      setDialog(null);
      toast({ title: `User ${user.email} deleted.` });
    } catch (err) {
      toastWriteFailure("Delete failed", err);
    }
  }

  async function onUnlinkGoogle(user: AdminUser) {
    try {
      await unlinkGoogle({ id: user.id });
      setDialog(null);
      toast({ title: `Google binding released for ${user.email}.` });
    } catch (err) {
      toastWriteFailure("Unlink failed", err);
    }
  }

  if (meLoading) {
    return <Skeleton className="h-40 w-full" />;
  }

  if (!isAdmin) {
    return (
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Admin — Users</h1>
        <p className="text-sm text-muted-foreground">
          You do not have permission to access this page.
        </p>
      </div>
    );
  }

  const filtered = (data?.users ?? []).filter(
    (u) =>
      u.email.toLowerCase().includes(search.toLowerCase()) ||
      u.name.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div>
      <div className="mb-6 flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Admin — Users</h1>
        <Input
          className="max-w-xs"
          placeholder="Search..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Email</TableHead>
              <TableHead>Name</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Created</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((user) => (
              <TableRow key={user.id}>
                <TableCell className="font-medium">{user.email}</TableCell>
                <TableCell>{user.name}</TableCell>
                <TableCell>
                  <RoleSelect user={user} />
                </TableCell>
                <TableCell>{formatDate(user.created_at, tz)}</TableCell>
                <TableCell>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => setDialog({ kind: "rename", user })}
                      aria-label="Edit name"
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon" aria-label="More actions">
                          <MoreHorizontal className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem
                          onClick={() => setDialog({ kind: "tokens", user })}
                        >
                          Manage tokens
                        </DropdownMenuItem>
                        {/* Releasing the only authentication method is refused
                            409 GOOGLE_IS_ONLY_AUTH_METHOD, so a password-less
                            row offers the item disabled rather than not at all.
                            The handler is `onSelect` because that is the hook
                            Radix gates on `disabled`. */}
                        {user.has_google && (
                          <DropdownMenuItem
                            disabled={!user.has_password}
                            title={
                              user.has_password
                                ? undefined
                                : "This account has no password, so releasing the binding would leave it unauthenticatable. The address's holder completes a password reset first; the unlink then succeeds."
                            }
                            onSelect={() => setDialog({ kind: "unlink-google", user })}
                          >
                            Unlink Google
                          </DropdownMenuItem>
                        )}
                        <DropdownMenuItem
                          className="text-destructive focus:text-destructive"
                          onClick={() => setDialog({ kind: "delete", user })}
                        >
                          Delete user
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {/* Rename dialog */}
      <Dialog
        open={dialog?.kind === "rename"}
        onOpenChange={(open) => !open && setDialog(null)}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Edit name</DialogTitle>
            <DialogDescription>{dialog?.kind === "rename" && dialog.user.email}</DialogDescription>
          </DialogHeader>
          {dialog?.kind === "rename" && (
            <RenameDialog user={dialog.user} onClose={() => setDialog(null)} />
          )}
        </DialogContent>
      </Dialog>

      {/* Tokens dialog */}
      <Dialog
        open={dialog?.kind === "tokens"}
        onOpenChange={(open) => !open && setDialog(null)}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>API tokens</DialogTitle>
            <DialogDescription>{dialog?.kind === "tokens" && dialog.user.email}</DialogDescription>
          </DialogHeader>
          {dialog?.kind === "tokens" && (
            <UserTokensDialog user={dialog.user} onClose={() => setDialog(null)} />
          )}
        </DialogContent>
      </Dialog>

      {/* Delete confirm dialog */}
      <ConfirmDialog
        open={dialog?.kind === "delete"}
        onOpenChange={(open) => !open && setDialog(null)}
        title="Delete user"
        description={
          dialog?.kind === "delete"
            ? `Permanently delete ${dialog.user.email}? This cannot be undone.`
            : "Permanently delete this user?"
        }
        confirmLabel="Delete"
        onConfirm={() => dialog?.kind === "delete" && onDeleteUser(dialog.user)}
        loading={deleting}
      />

      {/* Unlink Google confirm dialog */}
      <ConfirmDialog
        open={dialog?.kind === "unlink-google"}
        onOpenChange={(open) => !open && setDialog(null)}
        title="Unlink Google"
        description={
          dialog?.kind === "unlink-google"
            ? `Release the Google binding for ${dialog.user.email}? They are signed out of every session and sign in again. The next Google sign-in at that address binds afresh.`
            : "Release this user's Google binding?"
        }
        confirmLabel="Unlink"
        onConfirm={() => dialog?.kind === "unlink-google" && onUnlinkGoogle(dialog.user)}
        loading={unlinking}
      />
    </div>
  );
}

"use client";

import { useState, Suspense } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useConfirmPasswordReset } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/forms/field";
import { PasswordInput } from "@/components/forms/password-input";
import { resetPasswordSchema } from "./reset-password.schema";

const schema = resetPasswordSchema;

type FormValues = z.infer<typeof schema>;

function ResetPasswordForm() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token") ?? "";
  const [done, setDone] = useState(false);
  const { mutateAsync: confirmReset, isPending } = useConfirmPasswordReset();

  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  async function onSubmit(values: FormValues) {
    if (!token) {
      toast({ variant: "destructive", title: "Invalid link", description: "The reset token is missing from the URL." });
      return;
    }
    try {
      await confirmReset({ token, new_password: values.new_password });
      setDone(true);
    } catch (err) {
      if (err instanceof ApiError) {
        toast({ variant: "destructive", title: "Reset failed", description: err.message });
        if (err.error_code === "INVALID_RESET_TOKEN") {
          setError("new_password", { message: err.message });
        }
      } else {
        toast({ variant: "destructive", title: "Reset failed", description: "An unexpected error occurred." });
      }
    }
  }

  if (!token) {
    return (
      <div className="rounded-lg border bg-card p-8 shadow-sm">
        <h1 className="mb-4 text-2xl font-semibold tracking-tight">Invalid link</h1>
        <p className="text-sm text-muted-foreground">
          This password reset link is invalid or has already been used.
        </p>
        <div className="mt-6 text-center text-sm">
          <Link href="/forgot-password" className="text-primary underline-offset-4 hover:underline">
            Request a new reset link
          </Link>
        </div>
      </div>
    );
  }

  if (done) {
    return (
      <div className="rounded-lg border bg-card p-8 shadow-sm">
        <h1 className="mb-4 text-2xl font-semibold tracking-tight">Password updated</h1>
        <p className="text-sm text-muted-foreground">
          Your password has been changed. You can now sign in with your new password.
        </p>
        <div className="mt-6 text-center text-sm">
          <Link href="/login" className="text-primary underline-offset-4 hover:underline">
            Sign in
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-8 shadow-sm">
      <h1 className="mb-2 text-2xl font-semibold tracking-tight">Set new password</h1>
      <p className="mb-6 text-sm text-muted-foreground">
        Choose a new password for your account.
      </p>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
        <Field
          label="New password"
          htmlFor="new_password"
          error={errors.new_password?.message}
          hint="Minimum 10 characters"
          required
        >
          <PasswordInput
            id="new_password"
            autoComplete="new-password"
            {...register("new_password")}
          />
        </Field>

        <Button type="submit" className="w-full" disabled={isPending}>
          {isPending ? "Updating..." : "Update password"}
        </Button>
      </form>
    </div>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<div className="rounded-lg border bg-card p-8 shadow-sm" />}>
      <ResetPasswordForm />
    </Suspense>
  );
}

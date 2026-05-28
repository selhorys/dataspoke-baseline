"use client";

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import Link from "next/link";
import { useRequestPasswordReset } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/forms/field";
import { forgotPasswordSchema } from "./forgot-password.schema";

const schema = forgotPasswordSchema;

type FormValues = z.infer<typeof schema>;

export default function ForgotPasswordPage() {
  const [submitted, setSubmitted] = useState(false);
  const { mutateAsync: requestReset, isPending } = useRequestPasswordReset();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  async function onSubmit(values: FormValues) {
    try {
      await requestReset(values);
      setSubmitted(true);
    } catch (err) {
      if (err instanceof ApiError) {
        toast({ variant: "destructive", title: "Request failed", description: err.message });
      } else {
        toast({ variant: "destructive", title: "Request failed", description: "An unexpected error occurred." });
      }
    }
  }

  if (submitted) {
    return (
      <div className="rounded-lg border bg-card p-8 shadow-sm">
        <h1 className="mb-4 text-2xl font-semibold tracking-tight">Check your email</h1>
        <p className="text-sm text-muted-foreground">
          If that email address is registered, a password reset link has been sent. Check your inbox and follow the link.
        </p>
        <div className="mt-6 text-center text-sm">
          <Link href="/login" className="text-primary underline-offset-4 hover:underline">
            Back to sign in
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-lg border bg-card p-8 shadow-sm">
      <h1 className="mb-2 text-2xl font-semibold tracking-tight">Forgot password</h1>
      <p className="mb-6 text-sm text-muted-foreground">
        Enter your email address and we will send you a reset link.
      </p>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
        <Field label="Email" htmlFor="email" error={errors.email?.message} required>
          <Input
            id="email"
            type="email"
            autoComplete="email"
            placeholder="you@example.com"
            {...register("email")}
          />
        </Field>

        <Button type="submit" className="w-full" disabled={isPending}>
          {isPending ? "Sending..." : "Send reset link"}
        </Button>
      </form>

      <div className="mt-4 text-center text-sm">
        <Link href="/login" className="text-muted-foreground underline-offset-4 hover:underline">
          Back to sign in
        </Link>
      </div>
    </div>
  );
}

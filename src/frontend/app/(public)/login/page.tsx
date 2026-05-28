"use client";

import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { useLogin, getGoogleLoginUrl } from "@/lib/api/auth";
import { useAuthStore } from "@/lib/auth/store";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/forms/field";
import { PasswordInput } from "@/components/forms/password-input";

const schema = z.object({
  email: z.string().min(1, "Email is required"),
  password: z.string().min(1, "Password is required"),
});

type FormValues = z.infer<typeof schema>;

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const setToken = useAuthStore((s) => s.setToken);
  const { mutateAsync: login, isPending } = useLogin();

  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  async function onSubmit(values: FormValues) {
    try {
      const data = await login(values);
      setToken(data.access_token);
      const next = searchParams.get("next") ?? "/governance/dashboard";
      router.replace(next);
    } catch (err) {
      if (err instanceof ApiError) {
        toast({ variant: "destructive", title: "Sign in failed", description: err.message });
        setError("password", { message: err.message });
      } else {
        toast({ variant: "destructive", title: "Sign in failed", description: "An unexpected error occurred." });
      }
    }
  }

  return (
    <div className="rounded-lg border bg-card p-8 shadow-sm">
      <h1 className="mb-6 text-2xl font-semibold tracking-tight">Sign in</h1>

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

        <Field label="Password" htmlFor="password" error={errors.password?.message} required>
          <PasswordInput
            id="password"
            autoComplete="current-password"
            {...register("password")}
          />
        </Field>

        <Button type="submit" className="w-full" disabled={isPending}>
          {isPending ? "Signing in..." : "Sign in"}
        </Button>
      </form>

      <div className="my-4 flex items-center gap-2 text-xs text-muted-foreground">
        <span className="flex-1 border-t" />
        <span>or</span>
        <span className="flex-1 border-t" />
      </div>

      <GoogleButton />

      <div className="mt-4 space-y-1 text-sm text-center">
        <p>
          Need an account?{" "}
          <Link href="/register" className="text-primary underline-offset-4 hover:underline">
            Register
          </Link>
        </p>
        <p>
          <Link href="/forgot-password" className="text-muted-foreground underline-offset-4 hover:underline">
            Forgot password?
          </Link>
        </p>
      </div>
    </div>
  );
}

function GoogleButton() {
  const url = getGoogleLoginUrl();
  return (
    <a href={url} className="block w-full">
      <Button type="button" variant="outline" className="w-full">
        Sign in with Google
      </Button>
    </a>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="rounded-lg border bg-card p-8 shadow-sm" />}>
      <LoginForm />
    </Suspense>
  );
}

"use client";

import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRegister, getGoogleLoginUrl } from "@/lib/api/auth";
import { useAuthStore } from "@/lib/auth/store";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/forms/field";
import { PasswordInput } from "@/components/forms/password-input";
import { registerSchema } from "./register.schema";

const schema = registerSchema;

type FormValues = z.infer<typeof schema>;

export default function RegisterPage() {
  const router = useRouter();
  const setToken = useAuthStore((s) => s.setToken);
  const { mutateAsync: register, isPending } = useRegister();

  const {
    register: field,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  async function onSubmit(values: FormValues) {
    try {
      const data = await register(values);
      setToken(data.access_token);
      router.replace("/governance/dashboard");
    } catch (err) {
      if (err instanceof ApiError) {
        toast({ variant: "destructive", title: "Registration failed", description: err.message });
        if (err.error_code === "EMAIL_ALREADY_REGISTERED") {
          setError("email", { message: "This email is already registered." });
        }
      } else {
        toast({ variant: "destructive", title: "Registration failed", description: "An unexpected error occurred." });
      }
    }
  }

  return (
    <div className="rounded-lg border bg-card p-8 shadow-sm">
      <h1 className="mb-6 text-2xl font-semibold tracking-tight">Create account</h1>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
        <Field label="Email" htmlFor="email" error={errors.email?.message} required>
          <Input
            id="email"
            type="email"
            autoComplete="email"
            placeholder="you@example.com"
            {...field("email")}
          />
        </Field>

        <Field label="Name" htmlFor="name" error={errors.name?.message} required>
          <Input
            id="name"
            type="text"
            autoComplete="name"
            placeholder="Your name"
            {...field("name")}
          />
        </Field>

        <Field
          label="Password"
          htmlFor="password"
          error={errors.password?.message}
          hint="Minimum 10 characters"
          required
        >
          <PasswordInput
            id="password"
            autoComplete="new-password"
            {...field("password")}
          />
        </Field>

        <Button type="submit" className="w-full" disabled={isPending}>
          {isPending ? "Creating account..." : "Create account"}
        </Button>
      </form>

      <div className="my-4 flex items-center gap-2 text-xs text-muted-foreground">
        <span className="flex-1 border-t" />
        <span>or</span>
        <span className="flex-1 border-t" />
      </div>

      <a href={getGoogleLoginUrl()} className="block w-full">
        <Button type="button" variant="outline" className="w-full">
          Sign up with Google
        </Button>
      </a>

      <p className="mt-4 text-center text-sm">
        Already have an account?{" "}
        <Link href="/login" className="text-primary underline-offset-4 hover:underline">
          Sign in
        </Link>
      </p>
    </div>
  );
}

"use client";

import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMe } from "@/lib/auth/use-me";
import { useUpdateProfile } from "@/lib/api/auth";
import { ApiError } from "@/lib/api/client";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/forms/field";
import { PasswordInput } from "@/components/forms/password-input";
import { Skeleton } from "@/components/ui/skeleton";

const schema = z
  .object({
    name: z.string().min(1, "Name is required").max(128, "Name is too long"),
    password: z
      .string()
      .max(128, "Password is too long")
      .refine((v) => v === "" || v.length >= 10, {
        message: "Password must be at least 10 characters",
      })
      .optional(),
  })
  .transform((v) => ({
    name: v.name,
    password: v.password && v.password.length > 0 ? v.password : undefined,
  }));

type FormValues = {
  name: string;
  password?: string;
};

export default function ProfilePage() {
  const { me, isLoading } = useMe();
  const { mutateAsync: updateProfile, isPending } = useUpdateProfile();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: "", password: "" },
  });

  useEffect(() => {
    if (me) {
      reset({ name: me.name, password: "" });
    }
  }, [me, reset]);

  async function onSubmit(values: FormValues) {
    const payload: { name?: string; password?: string } = {};
    if (values.name !== me?.name) payload.name = values.name;
    if (values.password) payload.password = values.password;
    if (Object.keys(payload).length === 0) {
      toast({ title: "No changes to save." });
      return;
    }
    try {
      await updateProfile(payload);
      reset({ name: values.name, password: "" });
      toast({ title: "Profile updated." });
    } catch (err) {
      if (err instanceof ApiError) {
        toast({ variant: "destructive", title: "Update failed", description: err.message });
      } else {
        toast({ variant: "destructive", title: "Update failed", description: "An unexpected error occurred." });
      }
    }
  }

  if (isLoading) {
    return (
      <div className="max-w-lg space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
      </div>
    );
  }

  return (
    <div className="max-w-lg">
      <h1 className="mb-6 text-2xl font-semibold tracking-tight">Profile</h1>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
        <Field label="Email">
          <Input value={me?.email ?? ""} disabled />
        </Field>

        <Field label="Role">
          <Input value={me?.role ?? ""} disabled />
        </Field>

        <Field label="Google" >
          <Input value={me?.has_google ? "Linked" : "Not linked"} disabled />
        </Field>

        <Field label="Name" htmlFor="name" error={errors.name?.message} required>
          <Input
            id="name"
            type="text"
            autoComplete="name"
            {...register("name")}
          />
        </Field>

        <div className="border-t pt-4">
          <p className="mb-3 text-sm font-medium">Change password</p>
          <Field
            label="New password"
            htmlFor="password"
            error={errors.password?.message}
            hint="Leave blank to keep your current password. Minimum 10 characters."
          >
            <PasswordInput
              id="password"
              autoComplete="new-password"
              {...register("password")}
            />
          </Field>
        </div>

        <Button type="submit" disabled={isPending}>
          {isPending ? "Saving..." : "Save changes"}
        </Button>
      </form>
    </div>
  );
}

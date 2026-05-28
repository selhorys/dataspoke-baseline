import { z } from "zod";

export const resetPasswordSchema = z.object({
  new_password: z
    .string()
    .min(10, "Password must be at least 10 characters")
    .max(128, "Password is too long"),
});

export type ResetPasswordFormValues = z.infer<typeof resetPasswordSchema>;

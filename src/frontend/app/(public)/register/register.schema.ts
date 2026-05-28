import { z } from "zod";

export const registerSchema = z.object({
  email: z.string().email("Enter a valid email address"),
  name: z.string().min(1, "Name is required").max(128, "Name is too long"),
  password: z
    .string()
    .min(10, "Password must be at least 10 characters")
    .max(128, "Password is too long"),
});

export type RegisterFormValues = z.infer<typeof registerSchema>;

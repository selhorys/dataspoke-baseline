/**
 * Hand-written API types for the current phase.
 *
 * Run `pnpm codegen` against a live backend to regenerate `lib/api/types.generated.ts`
 * from the OpenAPI schema. Until then, only the types currently needed are defined here.
 */

export type UserRole = "Admin" | "Editor" | "Reader";

export interface Me {
  id: string;
  email: string;
  name: string;
  role: UserRole;
  has_google: boolean;
  created_at: string;
  updated_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface ApiErrorPayload {
  error_code: string;
  message: string;
  trace_id: string;
  resp_time: string;
}

// ── API Tokens ────────────────────────────────────────────────────────────────

export interface ApiTokenItem {
  id: string;
  name: string;
  role_snapshot: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
}

export interface ApiTokenListResponse {
  tokens: ApiTokenItem[];
  total: number;
}

export interface ApiTokenMintResponse {
  id: string;
  name: string;
  role_snapshot: string;
  token: string;
  created_at: string;
  expires_at: string | null;
}

// ── Admin Users ───────────────────────────────────────────────────────────────

export interface AdminUser {
  id: string;
  email: string;
  name: string;
  role: UserRole;
  has_google: boolean;
  created_at: string;
  updated_at: string;
}

export interface UsersListResponse {
  users: AdminUser[];
  total: number;
}

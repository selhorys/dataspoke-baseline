"""Authentication request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class TokenRequest(BaseModel):
    # str (not EmailStr) to allow the built-in bootstrap admin login
    # (email="dataspoke") in addition to normal email-addressed users.
    # Registration uses EmailStr to enforce valid email format.
    email: str = Field(max_length=254, description="User email address or bootstrap admin login name")
    password: str = Field(description="User password")


class TokenResponse(BaseModel):
    access_token: str = Field(description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type, always 'bearer'")
    expires_in: int = Field(description="Token lifetime in seconds")


class RefreshRequest(BaseModel):
    # Refresh token is read from the HttpOnly cookie by the route handler.
    pass


class RevokeRequest(BaseModel):
    # Refresh token is read from the HttpOnly cookie by the route handler.
    pass


# ── Registration ───────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    email: EmailStr = Field(max_length=254, description="User email address")
    name: str = Field(max_length=128, description="Display name")
    password: str = Field(min_length=10, max_length=128, description="Password (minimum 10 characters)")


# ── Profile ───────────────────────────────────────────────────────────────────


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    has_google: bool
    role: str
    created_at: datetime
    updated_at: datetime


class MePatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, min_length=10, max_length=128)


# ── Password reset ────────────────────────────────────────────────────────────


class PasswordResetRequest(BaseModel):
    email: EmailStr = Field(max_length=254, description="Email address to send reset token to")


class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(max_length=512, description="Reset token received by email")
    new_password: str = Field(min_length=10, max_length=128, description="New password (minimum 10 characters)")


# ── API tokens ─────────────────────────────────────────────────────────────────


class ApiTokenItem(BaseModel):
    id: uuid.UUID
    name: str
    role_snapshot: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class ApiTokenListResponse(BaseModel):
    tokens: list[ApiTokenItem]
    total: int


class ApiTokenMintRequest(BaseModel):
    name: str = Field(max_length=128, description="Descriptive name for this token")
    expires_at: datetime | None = None


class ApiTokenMintResponse(BaseModel):
    id: uuid.UUID
    name: str
    role_snapshot: str
    token: str = Field(description="Raw dsk_ token — only returned once")
    created_at: datetime
    expires_at: datetime | None = None

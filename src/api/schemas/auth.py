from pydantic import BaseModel, Field


class TokenRequest(BaseModel):
    email: str = Field(description="User email address")
    password: str = Field(description="User password")


class TokenResponse(BaseModel):
    access_token: str = Field(description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type, always 'bearer'")
    expires_in: int = Field(description="Token lifetime in seconds")


class RefreshRequest(BaseModel):
    # Refresh token is read from the HttpOnly cookie by the route handler.
    # This model is a placeholder in case the request carries extra body fields.
    pass


class RevokeRequest(BaseModel):
    # Refresh token is read from the HttpOnly cookie by the route handler.
    pass

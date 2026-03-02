from pydantic import BaseModel


class TokenRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    # Refresh token is read from the HttpOnly cookie by the route handler.
    # This model is a placeholder in case the request carries extra body fields.
    pass


class RevokeRequest(BaseModel):
    # Refresh token is read from the HttpOnly cookie by the route handler.
    pass

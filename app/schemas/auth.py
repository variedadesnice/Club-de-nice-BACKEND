from typing import Optional

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    email: str
    password: str = Field(..., min_length=6, description="Mínimo 6 caracteres")
    role: str = Field(default="miembro")


class LoginRequest(BaseModel):
    email: str
    password: str


class AvatarUploadRequest(BaseModel):
    imageData: str


class ProfileUpdateRequest(BaseModel):
    name: str
    avatar: str
    bio: str
    gender: Optional[str] = None
    city: Optional[str] = None
    phone: Optional[str] = None
    birthdate: Optional[str] = None


# --- Response models ---

class UserOut(BaseModel):
    id: str
    name: str
    email: str
    role: str
    avatar: str
    bio: str
    subscription_status: Optional[str] = None


class AuthSuccessResponse(BaseModel):
    user: UserOut
    token: str


class AuthNoAutoLoginResponse(BaseModel):
    requiresEmailConfirmation: bool = False
    autoLogin: bool = False


class AvatarResponse(BaseModel):
    url: str

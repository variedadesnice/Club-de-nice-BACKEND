from typing import Optional

from pydantic import BaseModel, Field, field_validator


# /register es público: nunca debe poder auto-asignarse "admin".
ALLOWED_ROLES = {"miembro", "invitado"}


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    email: str
    password: str = Field(..., min_length=6, description="Mínimo 6 caracteres")
    role: str = Field(default="miembro")

    @field_validator("role")
    @classmethod
    def normalize_role(cls, v: str) -> str:
        # Los gates de suscripción (deps.get_active_user y permissions.ts) comparan
        # contra roles en minúscula — un "Invitado" con mayúscula rompe la exención.
        role = (v or "").strip().lower()
        if role not in ALLOWED_ROLES:
            raise ValueError(f"Rol inválido: {v}")
        return role


class LoginRequest(BaseModel):
    email: str
    password: str


class ResetPasswordRequest(BaseModel):
    access_token: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, description="Mínimo 6 caracteres")


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

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.deps import get_current_user
from app.core.rate_limit import rate_limiter
from app.schemas.auth import AvatarUploadRequest, LoginRequest, ProfileUpdateRequest, RegisterRequest, ResetPasswordRequest
from app.services import auth as auth_service

router = APIRouter()


@router.post("/register", status_code=201, dependencies=[Depends(rate_limiter(5, 600, "register"))])
def register(body: RegisterRequest):
    result = auth_service.register(body.name, body.email, body.password, body.role)
    if not result.get("auto_login"):
        return JSONResponse(status_code=201, content={"requiresEmailConfirmation": False, "autoLogin": False})
    return JSONResponse(status_code=201, content={"user": result["user"], "token": result["token"]})


@router.post("/login", dependencies=[Depends(rate_limiter(10, 60, "login"))])
def login(body: LoginRequest):
    return auth_service.login(body.email, body.password)


@router.post("/reset-password", dependencies=[Depends(rate_limiter(5, 600, "reset-password"))])
def reset_password(body: ResetPasswordRequest):
    """Público — completa la recuperación de contraseña usando el access_token del enlace de recuperación."""
    return auth_service.reset_password(body.access_token, body.new_password)


@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    """Devuelve el perfil actual (incluye subscription_status) para refrescar el estado de cuenta."""
    return auth_service.get_me(current_user["id"], current_user["email"])


@router.post("/avatar")
def upload_avatar(body: AvatarUploadRequest, current_user: dict = Depends(get_current_user)):
    return auth_service.upload_avatar(current_user["id"], body.imageData)


@router.put("/profile")
def update_profile(body: ProfileUpdateRequest, current_user: dict = Depends(get_current_user)):
    return auth_service.update_profile(
        current_user["id"], body.name, body.avatar, body.bio,
        body.gender, body.city, body.phone, body.birthdate,
    )

from fastapi import APIRouter, Depends, Header, HTTPException

from app.core.deps import get_current_user
from app.services import streaks as streaks_service

router = APIRouter()


def _extract_token(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Formato de token inválido. Usar: Bearer <token>")
    return authorization.removeprefix("Bearer ")


@router.get("/checkin")
def checkin(
    authorization: str = Header(...),
    current_user: dict = Depends(get_current_user),
):
    """Registra el login diario del usuario llamando al RPC register_daily_login()."""
    token = _extract_token(authorization)
    return streaks_service.checkin(token, current_user["id"])


@router.get("/me")
def get_my_streak(current_user: dict = Depends(get_current_user)):
    return streaks_service.get_my_streak(current_user["id"])

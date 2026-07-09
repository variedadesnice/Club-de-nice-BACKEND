from fastapi import APIRouter, Depends, Header, HTTPException

from app.core.deps import get_current_user
from app.services import profile as profile_service

router = APIRouter()


def _extract_token(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Formato de token inválido. Usar: Bearer <token>")
    return authorization.removeprefix("Bearer ")


@router.get("/me/summary")
def get_my_summary(authorization: str = Header(...), current_user: dict = Depends(get_current_user)):
    """
    Agregador de perfil propio: nivel, insignias, racha, cursos completados
    e impacto social en una sola petición, en vez de 5 por separado.
    """
    token = _extract_token(authorization)
    return profile_service.get_my_summary(current_user["id"], token)

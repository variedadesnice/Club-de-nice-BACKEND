from fastapi import APIRouter, Depends

from app.core.deps import get_current_admin
from app.services import members as members_service

router = APIRouter()


@router.get("/")
def list_members(current_user: dict = Depends(get_current_admin)):
    """Admin — roster de miembros con estado de acceso (vencidos, por vencer, etc.)."""
    return members_service.list_members()

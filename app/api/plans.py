from fastapi import APIRouter

from app.services import plans as plans_service

router = APIRouter()


@router.get("/")
def get_plans():
    """Público — lista planes activos (is_active = true), ordenados por sort_order."""
    return plans_service.get_active_plans()

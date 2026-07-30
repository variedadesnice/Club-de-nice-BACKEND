from fastapi import APIRouter, Depends

from app.core.deps import get_current_admin
from app.schemas.plans import PlanCreate, PlanUpdate
from app.services import plans as plans_service

router = APIRouter()


@router.get("/")
def admin_get_plans(current_user: dict = Depends(get_current_admin)):
    """Admin — lista todos los planes (activos e inactivos)."""
    return plans_service.admin_get_all_plans()


@router.post("/", status_code=201)
def admin_create_plan(body: PlanCreate, current_user: dict = Depends(get_current_admin)):
    """Admin — crea un plan nuevo (code, name, sublabel?, duration_days?, price_usd, ...)."""
    return plans_service.admin_create_plan(body.model_dump())


@router.patch("/{plan_id}")
def admin_update_plan(plan_id: str, body: PlanUpdate, current_user: dict = Depends(get_current_admin)):
    """Admin — edita un plan existente."""
    return plans_service.admin_update_plan(plan_id, body.model_dump(exclude_none=True))


@router.patch("/{plan_id}/toggle")
def admin_toggle_plan(plan_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin — activa/desactiva un plan."""
    return plans_service.admin_toggle_plan(plan_id)


@router.delete("/{plan_id}")
def admin_delete_plan(plan_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin — elimina un plan. Falla con 409 si tiene pagos asociados."""
    return plans_service.admin_delete_plan(plan_id)

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


# Columnas que aceptan NULL en la tabla: mandar null en el PATCH las limpia
# (duration_days = null es "indefinido", sin vencimiento). El resto se ignora
# si viene en null, para no vaciar por accidente un campo obligatorio.
NULLABLE_FIELDS = {"sublabel", "duration_days"}


@router.patch("/{plan_id}")
def admin_update_plan(plan_id: str, body: PlanUpdate, current_user: dict = Depends(get_current_admin)):
    """Admin — edita un plan existente. Solo toca los campos enviados."""
    sent = body.model_dump(exclude_unset=True)
    data = {k: v for k, v in sent.items() if v is not None or k in NULLABLE_FIELDS}
    return plans_service.admin_update_plan(plan_id, data)


@router.patch("/{plan_id}/toggle")
def admin_toggle_plan(plan_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin — activa/desactiva un plan."""
    return plans_service.admin_toggle_plan(plan_id)


@router.delete("/{plan_id}")
def admin_delete_plan(plan_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin — elimina un plan. Falla con 409 si tiene pagos asociados."""
    return plans_service.admin_delete_plan(plan_id)

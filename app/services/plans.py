import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

def _get_plan_or_404(supabase, plan_id: str) -> dict:
    try:
        resp = supabase.table("plans").select("*").eq("id", plan_id).maybe_single().execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans._get_or_404] FAILED id=%s [%s] %s", plan_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    # .maybe_single() en supabase-py a veces devuelve None directamente (no un
    # objeto con .data = None) cuando la consulta no matchea ninguna fila.
    data = resp.data if resp is not None else None
    if not data:
        raise HTTPException(status_code=404, detail="Plan no encontrado.")
    return data


# ---------------------------------------------------------------------------
# Público
# ---------------------------------------------------------------------------

def get_active_plans() -> list:
    """
    Returns:
        Planes activos ordenados por sort_order.
    Raises:
        HTTPException 500
    """
    logger.info("[plans.get_active] fetching active plans")
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("plans")
            .select("*")
            .eq("is_active", True)
            .order("sort_order")
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.get_active] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener planes: {msg}")
    return resp.data or []


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def admin_get_all_plans() -> list:
    """
    Returns:
        Todos los planes (activos e inactivos), ordenados por sort_order.
    Raises:
        HTTPException 500
    """
    logger.info("[plans.admin_get_all] fetching all plans")
    supabase = get_supabase()
    try:
        resp = supabase.table("plans").select("*").order("sort_order").execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.admin_get_all] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener planes: {msg}")
    return resp.data or []


def admin_create_plan(data: dict) -> dict:
    """
    Crea un plan nuevo. El code se normaliza a minúsculas.

    Returns:
        Plan creado.
    Raises:
        HTTPException 409 — ya existe un plan con ese code
        HTTPException 500
    """
    code = data["code"].strip().lower()
    logger.info("[plans.admin_create] code=%s", code)
    supabase = get_supabase()

    try:
        existing = supabase.table("plans").select("id").eq("code", code).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.admin_create] duplicate check FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if existing.data:
        raise HTTPException(status_code=409, detail=f"Ya existe un plan con el código '{code}'.")

    try:
        resp = supabase.table("plans").insert({
            "code": code,
            "name": data["name"].strip(),
            "sublabel": data.get("sublabel"),
            "duration_days": data.get("duration_days"),
            "price_usd": data["price_usd"],
            "is_active": data.get("is_active", True),
            "sort_order": data.get("sort_order", 0),
        }).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.admin_create] insert FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear plan: {msg}")

    logger.info("[plans.admin_create] OK id=%s", resp.data[0].get("id"))
    return resp.data[0]


def admin_update_plan(plan_id: str, data: dict) -> dict:
    """
    Raises:
        HTTPException 400 — sin campos
        HTTPException 404 — no encontrado
        HTTPException 409 — code duplicado
        HTTPException 500
    """
    logger.info("[plans.admin_update] plan_id=%s", plan_id)
    supabase = get_supabase()
    _get_plan_or_404(supabase, plan_id)

    if not data:
        raise HTTPException(status_code=400, detail="No se enviaron campos para actualizar.")

    if "code" in data:
        code = data["code"].strip().lower()
        try:
            existing = (
                supabase.table("plans")
                .select("id")
                .eq("code", code)
                .neq("id", plan_id)
                .execute()
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=supabase_error(exc))
        if existing.data:
            raise HTTPException(status_code=409, detail=f"Ya existe un plan con el código '{code}'.")
        data["code"] = code

    if "name" in data:
        data["name"] = data["name"].strip()

    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        resp = supabase.table("plans").update(data).eq("id", plan_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.admin_update] FAILED id=%s [%s] %s", plan_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al actualizar plan: {msg}")

    logger.info("[plans.admin_update] OK id=%s", plan_id)
    return resp.data[0]


def admin_toggle_plan(plan_id: str) -> dict:
    """
    Invierte is_active.

    Raises:
        HTTPException 404
        HTTPException 500
    """
    logger.info("[plans.admin_toggle] plan_id=%s", plan_id)
    supabase = get_supabase()
    plan = _get_plan_or_404(supabase, plan_id)

    new_status = not plan["is_active"]
    try:
        resp = supabase.table("plans").update({
            "is_active": new_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", plan_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.admin_toggle] FAILED id=%s [%s] %s", plan_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al cambiar estado del plan: {msg}")

    logger.info("[plans.admin_toggle] OK id=%s is_active=%s", plan_id, new_status)
    return resp.data[0]


def admin_delete_plan(plan_id: str) -> dict:
    """
    No permite eliminar un plan con pagos asociados (payments.plan == code).

    Raises:
        HTTPException 404
        HTTPException 409 — tiene pagos asociados
        HTTPException 500
    """
    logger.info("[plans.admin_delete] plan_id=%s", plan_id)
    supabase = get_supabase()
    plan = _get_plan_or_404(supabase, plan_id)

    try:
        in_use = (
            supabase.table("payments")
            .select("id")
            .eq("plan", plan["code"])
            .limit(1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.admin_delete] usage check FAILED id=%s [%s] %s", plan_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if in_use.data:
        raise HTTPException(
            status_code=409,
            detail="No se puede eliminar este plan porque tiene pagos asociados.",
        )

    try:
        supabase.table("plans").delete().eq("id", plan_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.admin_delete] FAILED id=%s [%s] %s", plan_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar plan: {msg}")

    logger.info("[plans.admin_delete] OK id=%s", plan_id)
    return {"deleted": True}


def get_plan_duration_days(supabase, plan_code: str) -> Optional[int]:
    """
    Duración en días del plan (None = indefinido, sin vencimiento). Usado por
    payments.approve_payment() para calcular expires_at.

    Raises:
        HTTPException 400 — no existe un plan con ese código
        HTTPException 500
    """
    try:
        resp = supabase.table("plans").select("duration_days").eq("code", plan_code).maybe_single().execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.get_duration_days] lookup FAILED code=%s [%s] %s", plan_code, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    # .maybe_single() en supabase-py a veces devuelve None directamente (no un
    # objeto con .data = None) cuando la consulta no matchea ninguna fila.
    data = resp.data if resp is not None else None
    if not data:
        raise HTTPException(status_code=400, detail=f"Plan desconocido: {plan_code}")
    return data.get("duration_days")


def validate_active_plan(supabase, plan_code: str) -> None:
    """
    Confirma que plan_code exista y esté activo. Usado al registrar/renovar
    (mismo criterio que la validación de payment_method_id/currency_id).

    Raises:
        HTTPException 400 — plan inexistente o inactivo
        HTTPException 500
    """
    try:
        resp = supabase.table("plans").select("id, is_active").eq("code", plan_code).maybe_single().execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[plans.validate_active] lookup FAILED code=%s [%s] %s", plan_code, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    # .maybe_single() en supabase-py a veces devuelve None directamente (no un
    # objeto con .data = None) cuando la consulta no matchea ninguna fila.
    data = resp.data if resp is not None else None
    if not data or not data.get("is_active"):
        raise HTTPException(status_code=400, detail=f"El plan '{plan_code}' no está disponible.")

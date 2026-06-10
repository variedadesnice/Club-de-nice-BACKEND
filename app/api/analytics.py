from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.deps import get_current_admin
from app.services import analytics as analytics_service

router = APIRouter()


@router.get("/overview")
def get_overview(current_user: dict = Depends(get_current_admin)):
    """Admin — resumen general en tiempo real (miembros + ingresos)."""
    return analytics_service.get_overview()


@router.get("/members")
def get_members(current_user: dict = Depends(get_current_admin)):
    """Admin — detalle de miembros: totales, género, ciudad y rango de edad."""
    return analytics_service.get_members_detail()


@router.get("/revenue")
def get_revenue(current_user: dict = Depends(get_current_admin)):
    """Admin — detalle de ingresos en tiempo real."""
    return analytics_service.get_revenue_detail()


@router.get("/history")
def get_history(
    from_date: Optional[date] = Query(None, description="Fecha inicio (default: hoy - 30 días)"),
    to_date: Optional[date] = Query(None, description="Fecha fin (default: hoy)"),
    limit: int = Query(30, ge=1, le=365),
    current_user: dict = Depends(get_current_admin),
):
    """Admin — histórico de snapshots diarios, ordenado por snapshot_date desc."""
    return analytics_service.get_history(from_date, to_date, limit)


@router.post("/snapshot")
def create_snapshot(current_user: dict = Depends(get_current_admin)):
    """Admin — fuerza la generación/actualización del snapshot del día actual."""
    return analytics_service.generate_snapshot()

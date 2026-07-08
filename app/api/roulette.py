import logging

from fastapi import APIRouter, Depends, Query

from app.core.deps import get_active_user, get_current_admin
from app.schemas.roulette import CreatePrizeRequest, UpdatePrizeRequest, UpdateSettingsRequest
from app.services import roulette as roulette_service

# Rutas admin — montadas bajo /api/admin/roulette
router = APIRouter()

# Rutas de miembros — montadas bajo /api/roulette
public_router = APIRouter()

logger = logging.getLogger(__name__)


@router.get("/")
def get_settings(current_user: dict = Depends(get_current_admin)):
    return roulette_service.get_admin_settings()


@router.patch("/")
def update_settings(body: UpdateSettingsRequest, current_user: dict = Depends(get_current_admin)):
    return roulette_service.set_active(body.is_active)


@router.post("/prizes", status_code=201)
def create_prize(body: CreatePrizeRequest, current_user: dict = Depends(get_current_admin)):
    return roulette_service.create_prize(body.label, body.color, body.weight)


@router.patch("/prizes/{prize_id}")
def update_prize(prize_id: str, body: UpdatePrizeRequest, current_user: dict = Depends(get_current_admin)):
    return roulette_service.update_prize(prize_id, body.label, body.color, body.weight)


@router.delete("/prizes/{prize_id}")
def delete_prize(prize_id: str, current_user: dict = Depends(get_current_admin)):
    return roulette_service.delete_prize(prize_id)


@router.get("/spins")
def list_spins(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_admin),
):
    return roulette_service.list_spins(limit, offset)


@public_router.get("/status")
def get_status(current_user: dict = Depends(get_active_user)):
    return roulette_service.get_status(current_user["id"])


@public_router.post("/spin")
def spin(current_user: dict = Depends(get_active_user)):
    return roulette_service.spin(current_user["id"])

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.deps import get_active_user, get_current_admin, require_service_role
from app.schemas.raffles import CreateRaffleRequest
from app.services import raffles as raffles_service

# Rutas admin — montadas bajo /api/admin/raffles
router = APIRouter()

# Rutas de miembros — montadas bajo /api/raffles
public_router = APIRouter()

logger = logging.getLogger(__name__)


class UploadImageRequest(BaseModel):
    imageData: str


@router.get("/")
def list_raffles(current_user: dict = Depends(get_current_admin)):
    return raffles_service.list_raffles(include_email=True)


@router.post("/image", status_code=201)
def upload_raffle_image(body: UploadImageRequest, current_user: dict = Depends(get_current_admin)):
    return {"url": raffles_service.upload_raffle_image(body.imageData)}


@router.post("/", status_code=201)
def create_raffle(body: CreateRaffleRequest, current_user: dict = Depends(get_current_admin)):
    return raffles_service.create_raffle(
        body.title, body.description, body.image_url, body.winner_count, body.draw_at, current_user["id"],
    )


@router.post("/{raffle_id}/draw")
def draw_raffle(raffle_id: str, current_user: dict = Depends(get_current_admin)):
    return raffles_service.draw_raffle(raffle_id, include_email=True)


@router.post("/draw-scheduled/cron")
def draw_scheduled_cron(_: None = Depends(require_service_role)):
    """Endpoint para pg_cron — sortea automáticamente los sorteos vencidos."""
    logger.info("[raffles.cron] draw-scheduled triggered by cron")
    return raffles_service.draw_scheduled_raffles()


@router.delete("/{raffle_id}")
def delete_raffle(raffle_id: str, current_user: dict = Depends(get_current_admin)):
    return raffles_service.delete_raffle(raffle_id)


@public_router.get("/active")
def get_active_raffle(current_user: dict = Depends(get_active_user)):
    return raffles_service.get_active_raffle()

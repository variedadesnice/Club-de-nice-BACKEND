import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.deps import get_active_user, get_current_admin
from app.schemas.promo_banners import CreatePromoBannerRequest, SetActiveRequest, UpdatePromoBannerRequest
from app.services import promo_banners as promo_banners_service

# Rutas admin — montadas bajo /api/admin/promo-banners
router = APIRouter()

# Rutas de miembros — montadas bajo /api/promo-banners
public_router = APIRouter()

logger = logging.getLogger(__name__)


class UploadImageRequest(BaseModel):
    imageData: str


@router.get("/")
def list_banners(current_user: dict = Depends(get_current_admin)):
    return promo_banners_service.list_banners()


@router.post("/image", status_code=201)
def upload_banner_image(body: UploadImageRequest, current_user: dict = Depends(get_current_admin)):
    return {"url": promo_banners_service.upload_banner_image(body.imageData)}


@router.post("/", status_code=201)
def create_banner(body: CreatePromoBannerRequest, current_user: dict = Depends(get_current_admin)):
    return promo_banners_service.create_banner(
        body.title, body.description, body.image_url, body.link_url, current_user["id"],
    )


@router.patch("/{banner_id}")
def update_banner(banner_id: str, body: UpdatePromoBannerRequest, current_user: dict = Depends(get_current_admin)):
    return promo_banners_service.update_banner(banner_id, body.title, body.description, body.image_url, body.link_url)


@router.patch("/{banner_id}/active")
def set_active(banner_id: str, body: SetActiveRequest, current_user: dict = Depends(get_current_admin)):
    return promo_banners_service.set_active(banner_id, body.is_active)


@router.delete("/{banner_id}")
def delete_banner(banner_id: str, current_user: dict = Depends(get_current_admin)):
    return promo_banners_service.delete_banner(banner_id)


@public_router.get("/active")
def get_active_banner(current_user: dict = Depends(get_active_user)):
    return promo_banners_service.get_active_banner()

from fastapi import APIRouter, Depends, Query

from app.core.deps import get_current_admin
from app.schemas.levels import (
    AdminAwardRequest,
    CreateAchievementTypeRequest,
    CreateLevelTierRequest,
    IconUploadRequest,
    UpdateAchievementTypeRequest,
    UpdateLevelTierRequest,
)
from app.services import levels as levels_service

# ── /admin/levels ─────────────────────────────────────────────────────────────

admin_levels_router = APIRouter()


@admin_levels_router.get("/users")
def admin_get_users_levels(current_user: dict = Depends(get_current_admin)):
    return levels_service.admin_get_users_levels()


@admin_levels_router.post("/award")
def admin_award(body: AdminAwardRequest, current_user: dict = Depends(get_current_admin)):
    return levels_service.admin_award(body.user_id, body.xp_amount, body.reason)


@admin_levels_router.get("/tiers")
def admin_get_tiers(current_user: dict = Depends(get_current_admin)):
    return levels_service.admin_get_tiers()


@admin_levels_router.post("/tiers", status_code=201)
def admin_create_tier(body: CreateLevelTierRequest, current_user: dict = Depends(get_current_admin)):
    return levels_service.admin_create_tier(body.model_dump(exclude_none=True))


@admin_levels_router.patch("/tiers/{tier_id}")
def admin_update_tier(tier_id: str, body: UpdateLevelTierRequest, current_user: dict = Depends(get_current_admin)):
    return levels_service.admin_update_tier(tier_id, body.model_dump(exclude_none=True))


@admin_levels_router.delete("/tiers/{tier_id}")
def admin_delete_tier(tier_id: str, current_user: dict = Depends(get_current_admin)):
    """Elimina un rango de nivel. Nadie lo 'tiene': el rango se calcula por nivel."""
    return levels_service.admin_delete_tier(tier_id)


@admin_levels_router.post("/tiers/icon")
def admin_upload_tier_icon(body: IconUploadRequest, current_user: dict = Depends(get_current_admin)):
    """Sube icono al bucket 'level-tier-icons'. Retorna { url }."""
    return levels_service.admin_upload_tier_icon(body.imageData)


# ── /admin/achievements ───────────────────────────────────────────────────────

admin_achievements_router = APIRouter()


@admin_achievements_router.get("/")
def admin_get_achievements(current_user: dict = Depends(get_current_admin)):
    return levels_service.admin_get_all_achievements()


@admin_achievements_router.post("/", status_code=201)
def admin_create_achievement(body: CreateAchievementTypeRequest, current_user: dict = Depends(get_current_admin)):
    return levels_service.admin_create_achievement(body.model_dump(exclude_none=True))


@admin_achievements_router.patch("/{achievement_id}")
def admin_update_achievement(
    achievement_id: str,
    body: UpdateAchievementTypeRequest,
    current_user: dict = Depends(get_current_admin),
):
    return levels_service.admin_update_achievement(achievement_id, body.model_dump(exclude_none=True))


@admin_achievements_router.delete("/{achievement_id}")
def admin_delete_achievement(
    achievement_id: str,
    force: bool = Query(False, description="Borrar aunque ya se haya otorgado, quitándosela a quien la tenga."),
    current_user: dict = Depends(get_current_admin),
):
    """
    Elimina una insignia. Si ya se otorgó, responde 409 en vez de quitársela a
    nadie por sorpresa; con ?force=true se borra igual.
    """
    return levels_service.admin_delete_achievement(achievement_id, force)


@admin_achievements_router.post("/icon")
def admin_upload_achievement_icon(body: IconUploadRequest, current_user: dict = Depends(get_current_admin)):
    """Sube icono al bucket 'achievement-icons'. Retorna { url }."""
    return levels_service.admin_upload_achievement_icon(body.imageData)

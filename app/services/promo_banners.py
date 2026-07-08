import base64
import logging
import re
from datetime import datetime, timezone

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)

_MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _ext_from_mime(mime_type: str) -> str:
    return _MIME_TO_EXT.get(mime_type, "jpg")


def upload_banner_image(image_data: str) -> str:
    match = re.match(r"^data:(.+);base64,(.+)$", image_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de imagen inválido")
    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    ext = _ext_from_mime(mime_type)
    path = f"promo-{datetime.now(timezone.utc).timestamp()}.{ext}"
    supabase = get_supabase()
    try:
        supabase.storage.from_("promo-banners").upload(
            path, raw_bytes,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[promo_banners.upload_image] FAILED path=%s [%s] %s", path, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error subiendo imagen: {msg}")
    return supabase.storage.from_("promo-banners").get_public_url(path)


def _map_banner(b: dict) -> dict:
    return {
        "id": b["id"],
        "title": b["title"],
        "description": b["description"],
        "image_url": b["image_url"],
        "link_url": b["link_url"],
        "is_active": b["is_active"],
        "created_at": b["created_at"],
    }


def list_banners() -> list:
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("promo_banners")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[promo_banners.list] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return [_map_banner(b) for b in (resp.data or [])]


def create_banner(title: str, description: str, image_url: str, link_url: str, created_by: str) -> dict:
    logger.info("[promo_banners.create] title=%s by=%s", title, created_by)
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("promo_banners")
            .insert({
                "title": title,
                "description": description,
                "image_url": image_url,
                "link_url": link_url,
                "created_by": created_by,
            })
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[promo_banners.create] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return _map_banner(resp.data[0])


def update_banner(banner_id: str, title: str | None, description: str | None, image_url: str | None, link_url: str | None) -> dict:
    supabase = get_supabase()
    updates = {}
    if title is not None:
        updates["title"] = title
    if description is not None:
        updates["description"] = description
    if image_url is not None:
        updates["image_url"] = image_url
    if link_url is not None:
        updates["link_url"] = link_url

    if not updates:
        raise HTTPException(status_code=400, detail="Nada para actualizar.")

    try:
        resp = supabase.table("promo_banners").update(updates).eq("id", banner_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[promo_banners.update] FAILED banner_id=%s: %s", banner_id, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not resp.data:
        raise HTTPException(status_code=404, detail="Banner no encontrado.")
    return _map_banner(resp.data[0])


def set_active(banner_id: str, is_active: bool) -> dict:
    """Solo puede haber un banner activo a la vez: activar uno desactiva los demás."""
    logger.info("[promo_banners.set_active] banner_id=%s is_active=%s", banner_id, is_active)
    supabase = get_supabase()

    try:
        exists = supabase.table("promo_banners").select("id").eq("id", banner_id).maybe_single().execute()
    except Exception as exc:
        msg = supabase_error(exc)
        raise HTTPException(status_code=500, detail=msg)
    if not exists.data:
        raise HTTPException(status_code=404, detail="Banner no encontrado.")

    if is_active:
        try:
            supabase.table("promo_banners").update({"is_active": False}).eq("is_active", True).neq("id", banner_id).execute()
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[promo_banners.set_active] deactivate-others FAILED: %s", msg, exc_info=True)
            raise HTTPException(status_code=500, detail=msg)

    try:
        resp = supabase.table("promo_banners").update({"is_active": is_active}).eq("id", banner_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[promo_banners.set_active] FAILED banner_id=%s: %s", banner_id, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    return _map_banner(resp.data[0])


def delete_banner(banner_id: str) -> dict:
    logger.info("[promo_banners.delete] banner_id=%s", banner_id)
    supabase = get_supabase()
    try:
        supabase.table("promo_banners").delete().eq("id", banner_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[promo_banners.delete] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return {"deleted": True}


def get_active_banner() -> dict | None:
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("promo_banners")
            .select("*")
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[promo_banners.get_active] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = resp.data or []
    if not rows:
        return None
    return _map_banner(rows[0])

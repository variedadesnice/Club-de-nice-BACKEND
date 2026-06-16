import logging

from fastapi import HTTPException

from app.core.cache import cache_delete, cache_get, cache_set
from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)

_TAGS_KEY = "tags:all"
_TAGS_TTL = 300  # 5 minutos


def get_tags() -> list:
    """
    Returns:
        Lista de {id, name}
    Raises:
        HTTPException 500 — fallo al consultar Supabase
    """
    cached = cache_get(_TAGS_KEY)
    if cached is not None:
        return cached

    logger.info("[tags.get_tags] fetching all tags")
    supabase = get_supabase()
    try:
        resp = supabase.table("tags").select("id, name").order("name").execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[tags.get_tags] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener tags: {msg}")
    result = resp.data or []
    cache_set(_TAGS_KEY, result, _TAGS_TTL)
    return result


def create_tag(name: str) -> dict:
    """
    Idempotente: devuelve el tag existente si ya existe.

    Returns:
        {id, name}
    Raises:
        HTTPException 500 — fallo al insertar
    """
    name = name.strip().lower()
    logger.info("[tags.create_tag] name=%s", name)
    supabase = get_supabase()

    try:
        existing = supabase.table("tags").select("id, name").eq("name", name).execute()
        if existing.data:
            logger.info("[tags.create_tag] tag already exists id=%s", existing.data[0].get("id"))
            return existing.data[0]
    except Exception as exc:
        logger.warning("[tags.create_tag] duplicate check failed name=%s [%s] %s", name, type(exc).__name__, supabase_error(exc))

    try:
        resp = supabase.table("tags").insert({"name": name}).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[tags.create_tag] insert FAILED name=%s [%s] %s", name, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear tag: {msg}")

    cache_delete(_TAGS_KEY)
    logger.info("[tags.create_tag] OK id=%s", resp.data[0].get("id"))
    return resp.data[0]


def delete_tag(tag_id: str) -> dict:
    """
    Elimina el tag y sus relaciones en post_tags.

    Returns:
        {"deleted": True}
    Raises:
        HTTPException 500 — fallo al eliminar
    """
    logger.info("[tags.delete_tag] tag_id=%s", tag_id)
    supabase = get_supabase()
    try:
        supabase.table("post_tags").delete().eq("tag_id", tag_id).execute()
        supabase.table("tags").delete().eq("id", tag_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[tags.delete_tag] FAILED tag_id=%s [%s] %s", tag_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar tag: {msg}")
    cache_delete(_TAGS_KEY)
    logger.info("[tags.delete_tag] OK tag_id=%s", tag_id)
    return {"deleted": True}

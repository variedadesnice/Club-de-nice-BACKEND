"""
Categorías de contenido (tabla content_categories, sql/content_categories.sql).

courses.category guarda el `name` de una categoría como texto, sin FK. Para que
no queden cursos apuntando a una categoría que ya no existe, este servicio es
quien mantiene los dos alineados: renombrar arrastra a los cursos y borrar se
niega mientras haya cursos en la categoría.
"""
import logging
from datetime import datetime, timezone

from fastapi import HTTPException

from app.core.cache import cache_delete
from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)

# Misma clave que courses.py / classroom.py: renombrar cambia courses.category.
_COURSES_CACHE_KEY = "courses:all"


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

def _get_or_404(supabase, category_id: str) -> dict:
    try:
        resp = supabase.table("content_categories").select("*").eq("id", category_id).maybe_single().execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[content_categories._get_or_404] FAILED id=%s [%s] %s", category_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    # .maybe_single() a veces devuelve None directamente cuando no hay fila.
    data = resp.data if resp is not None else None
    if not data:
        raise HTTPException(status_code=404, detail="Categoría no encontrada.")
    return data


def _ensure_name_free(supabase, name: str, exclude_id: str | None = None) -> None:
    """
    409 si otra categoría ya usa ese nombre, sin distinguir mayúsculas. Se
    compara en Python (son pocas filas) porque ilike tomaría '%', '_' o '*'
    del nombre como comodines.
    """
    try:
        resp = supabase.table("content_categories").select("id, name").execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[content_categories._ensure_name_free] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    wanted = name.strip().lower()
    if any(row["id"] != exclude_id and row["name"].strip().lower() == wanted for row in (resp.data or [])):
        raise HTTPException(status_code=409, detail=f"Ya existe una categoría llamada '{name}'.")


def _count_courses(supabase, name: str) -> int:
    try:
        resp = supabase.table("courses").select("id", count="exact").eq("category", name).limit(1).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[content_categories._count_courses] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return resp.count or 0


# ---------------------------------------------------------------------------
# Público
# ---------------------------------------------------------------------------

def get_active_categories() -> list:
    """
    Returns:
        Categorías activas ordenadas por sort_order.
    Raises:
        HTTPException 500
    """
    logger.info("[content_categories.get_active] fetching")
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("content_categories")
            .select("*")
            .eq("is_active", True)
            .order("sort_order")
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[content_categories.get_active] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener categorías: {msg}")
    return resp.data or []


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def admin_get_all() -> list:
    """
    Returns:
        Todas las categorías (activas e inactivas) por sort_order, cada una con
        `course_count` para que el panel sepa cuáles se pueden borrar.
    Raises:
        HTTPException 500
    """
    logger.info("[content_categories.admin_get_all] fetching")
    supabase = get_supabase()
    try:
        resp = supabase.table("content_categories").select("*").order("sort_order").execute()
        courses = supabase.table("courses").select("category").execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[content_categories.admin_get_all] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener categorías: {msg}")

    counts: dict[str, int] = {}
    for c in courses.data or []:
        counts[c.get("category")] = counts.get(c.get("category"), 0) + 1
    return [{**row, "course_count": counts.get(row["name"], 0)} for row in (resp.data or [])]


def admin_create(data: dict) -> dict:
    """
    Returns:
        Categoría creada.
    Raises:
        HTTPException 409 — ya existe una con ese nombre
        HTTPException 500
    """
    name = data["name"].strip()
    logger.info("[content_categories.admin_create] name=%s", name)
    supabase = get_supabase()
    _ensure_name_free(supabase, name)

    try:
        resp = supabase.table("content_categories").insert({
            "name": name,
            "description": (data.get("description") or "").strip() or None,
            "icon": data.get("icon", "play"),
            "sort_order": data.get("sort_order", 0),
            "is_active": data.get("is_active", True),
        }).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[content_categories.admin_create] insert FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear la categoría: {msg}")

    logger.info("[content_categories.admin_create] OK id=%s", resp.data[0].get("id"))
    return {**resp.data[0], "course_count": 0}


def admin_update(category_id: str, data: dict) -> dict:
    """
    Si cambia el nombre, renombra también courses.category de los cursos que
    la usaban — si no, esos cursos quedarían fuera de cualquier categoría.

    Raises:
        HTTPException 400 — sin campos
        HTTPException 404 — no encontrada
        HTTPException 409 — el nombre nuevo ya existe
        HTTPException 500
    """
    logger.info("[content_categories.admin_update] id=%s fields=%s", category_id, list(data))
    if not data:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar.")

    supabase = get_supabase()
    current = _get_or_404(supabase, category_id)

    updates = dict(data)
    if "name" in updates:
        updates["name"] = updates["name"].strip()
        if updates["name"].lower() != current["name"].lower():
            _ensure_name_free(supabase, updates["name"], exclude_id=category_id)
    if "description" in updates:
        updates["description"] = (updates["description"] or "").strip() or None
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        resp = supabase.table("content_categories").update(updates).eq("id", category_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[content_categories.admin_update] FAILED id=%s [%s] %s", category_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al actualizar la categoría: {msg}")

    new_name = updates.get("name", current["name"])
    if new_name != current["name"]:
        try:
            supabase.table("courses").update({"category": new_name}).eq("category", current["name"]).execute()
        except Exception as exc:
            # La categoría ya quedó renombrada; avisar fuerte para corregir a mano.
            logger.error(
                "[content_categories.admin_update] cursos NO renombrados '%s' -> '%s' [%s] %s",
                current["name"], new_name, type(exc).__name__, supabase_error(exc), exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"La categoría se renombró, pero sus cursos siguen como '{current['name']}'. Edítalos o vuelve a intentarlo.",
            )
        cache_delete(_COURSES_CACHE_KEY)

    row = resp.data[0]
    logger.info("[content_categories.admin_update] OK id=%s", category_id)
    return {**row, "course_count": _count_courses(supabase, row["name"])}


def admin_toggle(category_id: str) -> dict:
    """
    Activa/desactiva. Una categoría inactiva desaparece de los atajos y del
    filtro, pero sus cursos no se tocan.

    Raises:
        HTTPException 404/500
    """
    supabase = get_supabase()
    current = _get_or_404(supabase, category_id)
    new_active = not current["is_active"]
    logger.info("[content_categories.admin_toggle] id=%s is_active=%s", category_id, new_active)
    try:
        resp = (
            supabase.table("content_categories")
            .update({"is_active": new_active, "updated_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", category_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[content_categories.admin_toggle] FAILED id=%s [%s] %s", category_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    row = resp.data[0]
    return {**row, "course_count": _count_courses(supabase, row["name"])}


def admin_delete(category_id: str) -> dict:
    """
    Raises:
        HTTPException 404 — no encontrada
        HTTPException 409 — tiene cursos (moverlos antes, o desactivarla)
        HTTPException 500
    """
    supabase = get_supabase()
    current = _get_or_404(supabase, category_id)
    in_use = _count_courses(supabase, current["name"])
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"'{current['name']}' tiene {in_use} contenido(s). Muévelos a otra categoría o desactívala en vez de borrarla.",
        )

    logger.info("[content_categories.admin_delete] id=%s", category_id)
    try:
        supabase.table("content_categories").delete().eq("id", category_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[content_categories.admin_delete] FAILED id=%s [%s] %s", category_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return {"deleted": True}

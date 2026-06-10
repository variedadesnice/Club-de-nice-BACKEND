import base64
import logging
import re
import time
from typing import Optional

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase, is_supabase_configured

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

def _map_course(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "title": c.get("title"),
        "description": c.get("description"),
        "thumbnail": c.get("thumbnail"),
        "category": c.get("category"),
        "module": c.get("module"),
        "progress": c.get("progress"),
        "created_at": c.get("created_at"),
        "created_by": c.get("created_by"),
    }


def _map_chapter(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "courseId": c.get("course_id"),
        "title": c.get("title"),
        "videoUrl": c.get("video_url"),
        "duration": c.get("duration"),
        "sortOrder": c.get("sort_order"),
    }


def _sync_module_label(supabase, course_id: str) -> str:
    """Recalcula y actualiza el campo `module` del curso según sus capítulos."""
    try:
        chapters_resp = (
            supabase.table("course_chapters")
            .select("title")
            .eq("course_id", course_id)
            .order("sort_order")
            .execute()
        )
        chapters = chapters_resp.data or []
        count = len(chapters)
        if count == 0:
            module = "Sin capítulos"
        elif count == 1:
            module = chapters[0]["title"]
        else:
            module = f"{count} capítulos"
        supabase.table("courses").update({"module": module}).eq("id", course_id).execute()
    except Exception as exc:
        logger.warning("[courses._sync_module_label] FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, supabase_error(exc))
        module = "Sin capítulos"
    return module


# ---------------------------------------------------------------------------
# Servicios públicos
# ---------------------------------------------------------------------------

def get_courses() -> list:
    """
    Returns:
        Lista de cursos mapeados
    Raises:
        HTTPException 500
    """
    logger.info("[courses.get_courses] fetching all courses")
    supabase = get_supabase()
    try:
        resp = supabase.table("courses").select("*").order("created_at", desc=True).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[courses.get_courses] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener cursos: {msg}")
    return [_map_course(c) for c in (resp.data or [])]


def create_course(title: str, description: str, thumbnail: str, user_id: Optional[str], category: str) -> dict:
    """
    Intenta incluir `created_by`; si la columna no existe en la tabla, reintenta sin ella.

    Returns:
        Curso mapeado
    Raises:
        HTTPException 500
    """
    logger.info("[courses.create_course] title=%s userId=%s", title, user_id)
    supabase = get_supabase()

    course_data: dict = {
        "title": title,
        "description": description,
        "thumbnail": thumbnail,
        "category": category,
        "module": "Sin capítulos",
        "progress": 0,
    }
    if user_id:
        course_data["created_by"] = user_id

    try:
        resp = supabase.table("courses").insert(course_data).execute()
        course = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        if user_id and "created_by" in msg:
            logger.warning("[courses.create_course] created_by column missing, retrying without it")
            course_data.pop("created_by", None)
            try:
                resp = supabase.table("courses").insert(course_data).execute()
                course = resp.data[0]
            except Exception as exc2:
                msg2 = supabase_error(exc2)
                logger.error("[courses.create_course] retry FAILED [%s] %s", type(exc2).__name__, msg2, exc_info=True)
                raise HTTPException(status_code=500, detail=f"Error al crear curso: {msg2}")
        else:
            logger.error("[courses.create_course] insert FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al crear curso: {msg}")

    logger.info("[courses.create_course] OK course_id=%s", course.get("id"))
    return _map_course(course)


def upload_thumbnail(image_data: str) -> dict:
    """
    Sube la imagen a Supabase Storage. Si Supabase no está configurado o falla,
    devuelve la imagen inline (base64) si es menor a 800 KB.

    Returns:
        {"url": "...", "storage": "supabase" | "inline"}
    Raises:
        HTTPException 400 — formato inválido
        HTTPException 500 — imagen demasiado grande y fallo en Storage
    """
    logger.info("[courses.upload_thumbnail] imageData_len=%d", len(image_data))

    if not is_supabase_configured() and len(image_data) <= 800_000:
        logger.info("[courses.upload_thumbnail] Supabase not configured, returning inline")
        return {"url": image_data, "storage": "inline"}

    match = re.match(r"^data:(.+);base64,(.+)$", image_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de imagen inválido")

    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    supabase = get_supabase()
    path = f"course-{int(time.time() * 1000)}.jpg"

    try:
        supabase.storage.from_("Avatars").upload(
            path, raw_bytes,
            file_options={"content-type": mime_type, "upsert": "false"},
        )
        url = supabase.storage.from_("Avatars").get_public_url(path)
        logger.info("[courses.upload_thumbnail] OK path=%s", path)
        return {"url": url, "storage": "supabase"}
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[courses.upload_thumbnail] upload FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        if len(image_data) <= 800_000:
            logger.warning("[courses.upload_thumbnail] falling back to inline storage")
            return {"url": image_data, "storage": "inline"}
        raise HTTPException(status_code=500, detail=f"Error al subir thumbnail: {msg}")


def update_course(course_id: str, title: Optional[str], description: Optional[str], thumbnail: Optional[str], category: Optional[str]) -> dict:
    """
    Raises:
        HTTPException 404 — curso no encontrado
        HTTPException 500
    """
    logger.info("[courses.update_course] course_id=%s", course_id)
    supabase = get_supabase()

    updates: dict = {}
    if title is not None:
        updates["title"] = title
    if description is not None:
        updates["description"] = description
    if thumbnail is not None:
        updates["thumbnail"] = thumbnail
    if category is not None:
        updates["category"] = category

    if updates:
        try:
            supabase.table("courses").update(updates).eq("id", course_id).execute()
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[courses.update_course] update FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al actualizar curso: {msg}")

    try:
        resp = supabase.table("courses").select("*").eq("id", course_id).single().execute()
        if not resp.data:
            raise HTTPException(status_code=404, detail="Curso no encontrado")
    except HTTPException:
        raise
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[courses.update_course] fetch FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener curso actualizado: {msg}")

    logger.info("[courses.update_course] OK course_id=%s", course_id)
    return _map_course(resp.data)


def get_chapters(course_id: str) -> list:
    """
    Raises:
        HTTPException 500
    """
    logger.info("[courses.get_chapters] course_id=%s", course_id)
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("course_chapters")
            .select("*")
            .eq("course_id", course_id)
            .order("sort_order")
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[courses.get_chapters] FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener capítulos: {msg}")
    return [_map_chapter(c) for c in (resp.data or [])]


def create_chapter(course_id: str, title: str, video_url: Optional[str], duration: Optional[str]) -> dict:
    """
    Raises:
        HTTPException 500
    """
    logger.info("[courses.create_chapter] course_id=%s title=%s", course_id, title)
    supabase = get_supabase()

    try:
        existing = supabase.table("course_chapters").select("id").eq("course_id", course_id).execute()
        sort_order = len(existing.data) if existing.data else 0
    except Exception as exc:
        logger.warning("[courses.create_chapter] sort_order fetch FAILED course_id=%s, defaulting to 0 [%s]", course_id, supabase_error(exc))
        sort_order = 0

    try:
        resp = supabase.table("course_chapters").insert({
            "course_id": course_id, "title": title,
            "video_url": video_url, "duration": duration, "sort_order": sort_order,
        }).execute()
        chapter = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[courses.create_chapter] insert FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear capítulo: {msg}")

    _sync_module_label(supabase, course_id)
    logger.info("[courses.create_chapter] OK chapter_id=%s", chapter.get("id"))
    return _map_chapter(chapter)


def update_chapter(course_id: str, chapter_id: str, title: Optional[str], video_url: Optional[str], duration: Optional[str]) -> dict:
    """
    Raises:
        HTTPException 404/500
    """
    logger.info("[courses.update_chapter] course_id=%s chapter_id=%s", course_id, chapter_id)
    supabase = get_supabase()

    updates: dict = {}
    if title is not None:
        updates["title"] = title
    if video_url is not None:
        updates["video_url"] = video_url
    if duration is not None:
        updates["duration"] = duration

    if updates:
        try:
            supabase.table("course_chapters").update(updates).eq("id", chapter_id).execute()
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[courses.update_chapter] update FAILED chapter_id=%s [%s] %s", chapter_id, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al actualizar capítulo: {msg}")

    _sync_module_label(supabase, course_id)

    try:
        resp = supabase.table("course_chapters").select("*").eq("id", chapter_id).single().execute()
        if not resp.data:
            raise HTTPException(status_code=404, detail="Capítulo no encontrado")
    except HTTPException:
        raise
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[courses.update_chapter] fetch FAILED chapter_id=%s [%s] %s", chapter_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener capítulo actualizado: {msg}")

    logger.info("[courses.update_chapter] OK chapter_id=%s", chapter_id)
    return _map_chapter(resp.data)

import logging
from typing import Optional

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)


def _award(user_id: str, code: str, metadata: dict = None) -> None:
    """Fire-and-forget: otorga un logro sin bloquear ni lanzar excepciones."""
    try:
        from app.services.levels import process_achievement
        process_achievement(user_id, code, metadata)
    except Exception as exc:
        logger.warning("[classroom._award] silenced error user_id=%s code=%s [%s]", user_id, code, exc)


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
        "isPublished": c.get("is_published"),
        "createdBy": c.get("created_by"),
        "createdAt": c.get("created_at"),
        "updatedAt": c.get("updated_at"),
    }


def _map_chapter(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "courseId": c.get("course_id"),
        "title": c.get("title"),
        "description": c.get("description"),
        "videoUrl": c.get("video_url"),
        "duration": c.get("duration"),
        "sortOrder": c.get("sort_order"),
        "createdAt": c.get("created_at"),
        "updatedAt": c.get("updated_at"),
    }


def _progress_pct(completed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(completed / total * 100, 2)


def _get_course_or_404(supabase, course_id: str, require_published: bool = False) -> dict:
    try:
        resp = supabase.table("courses").select("*").eq("id", course_id).maybe_single().execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom._get_course_or_404] FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al buscar curso: {msg}")

    if not resp.data:
        raise HTTPException(status_code=404, detail="Curso no encontrado")

    if require_published and not resp.data.get("is_published"):
        raise HTTPException(status_code=404, detail="Curso no encontrado")

    return resp.data


def _chapter_counts(supabase, course_ids: list) -> dict:
    """Returns {course_id: total_chapters}."""
    if not course_ids:
        return {}
    try:
        resp = (
            supabase.table("course_chapters")
            .select("id, course_id")
            .in_("course_id", course_ids)
            .execute()
        )
    except Exception as exc:
        logger.warning("[classroom._chapter_counts] FAILED [%s] %s", type(exc).__name__, supabase_error(exc))
        return {}

    counts: dict = {}
    for row in (resp.data or []):
        cid = row["course_id"]
        counts[cid] = counts.get(cid, 0) + 1
    return counts


def _completed_counts(supabase, user_id: str, course_ids: list) -> dict:
    """Returns {course_id: completed_chapters} for the given user."""
    if not course_ids:
        return {}
    try:
        resp = (
            supabase.table("user_course_progress")
            .select("course_id, chapter_id, completed")
            .eq("user_id", user_id)
            .eq("completed", True)
            .in_("course_id", course_ids)
            .execute()
        )
    except Exception as exc:
        logger.warning("[classroom._completed_counts] FAILED [%s] %s", type(exc).__name__, supabase_error(exc))
        return {}

    counts: dict = {}
    for row in (resp.data or []):
        cid = row["course_id"]
        counts[cid] = counts.get(cid, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Servicios públicos (miembro)
# ---------------------------------------------------------------------------

def get_courses(user_id: str) -> list:
    """
    Lista cursos publicados con número de capítulos y progreso del usuario.

    Raises:
        HTTPException 500
    """
    logger.info("[classroom.get_courses] user_id=%s", user_id)
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("courses")
            .select("*")
            .eq("is_published", True)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.get_courses] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener cursos: {msg}")

    courses = resp.data or []
    course_ids = [c["id"] for c in courses]
    totals = _chapter_counts(supabase, course_ids)
    completed = _completed_counts(supabase, user_id, course_ids)

    result = []
    for c in courses:
        mapped = _map_course(c)
        total = totals.get(c["id"], 0)
        done = completed.get(c["id"], 0)
        mapped["chapterCount"] = total
        mapped["completedChapters"] = done
        mapped["progress"] = _progress_pct(done, total)
        result.append(mapped)
    return result


def get_course_detail(course_id: str, user_id: str) -> dict:
    """
    Detalle del curso con capítulos ordenados por sort_order y progreso del usuario.

    Raises:
        HTTPException 404 — curso no encontrado o no publicado
        HTTPException 500
    """
    logger.info("[classroom.get_course_detail] course_id=%s user_id=%s", course_id, user_id)
    supabase = get_supabase()
    course = _get_course_or_404(supabase, course_id, require_published=True)

    try:
        chapters_resp = (
            supabase.table("course_chapters")
            .select("*")
            .eq("course_id", course_id)
            .order("sort_order")
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.get_course_detail] chapters FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener capítulos: {msg}")

    chapters = chapters_resp.data or []

    try:
        progress_resp = (
            supabase.table("user_course_progress")
            .select("chapter_id, completed, completed_at")
            .eq("user_id", user_id)
            .eq("course_id", course_id)
            .execute()
        )
        progress_by_chapter = {
            row["chapter_id"]: row for row in (progress_resp.data or []) if row.get("completed")
        }
    except Exception as exc:
        logger.warning("[classroom.get_course_detail] progress FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, supabase_error(exc))
        progress_by_chapter = {}

    mapped_chapters = []
    completed_count = 0
    for ch in chapters:
        mapped = _map_chapter(ch)
        progress_row = progress_by_chapter.get(ch["id"])
        mapped["completed"] = bool(progress_row)
        mapped["completedAt"] = progress_row.get("completed_at") if progress_row else None
        if progress_row:
            completed_count += 1
        mapped_chapters.append(mapped)

    result = _map_course(course)
    result["chapters"] = mapped_chapters
    result["progress"] = _progress_pct(completed_count, len(chapters))
    return result


def complete_chapter(course_id: str, chapter_id: str, user_id: str) -> dict:
    """
    Marca un capítulo como completado (inserta o actualiza user_course_progress).

    Otorga logro `lesson_completed` por cada capítulo nuevo completado, y
    `course_completed` si con esto el usuario completa todos los capítulos del curso.

    Raises:
        HTTPException 404 — curso o capítulo no encontrado
        HTTPException 500
    """
    logger.info("[classroom.complete_chapter] course_id=%s chapter_id=%s user_id=%s", course_id, chapter_id, user_id)
    supabase = get_supabase()
    _get_course_or_404(supabase, course_id)

    try:
        chapter_resp = (
            supabase.table("course_chapters")
            .select("id")
            .eq("id", chapter_id)
            .eq("course_id", course_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.complete_chapter] chapter lookup FAILED course_id=%s chapter_id=%s [%s] %s", course_id, chapter_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al buscar capítulo: {msg}")

    if not chapter_resp.data:
        raise HTTPException(status_code=404, detail="Capítulo no encontrado")

    try:
        existing_resp = (
            supabase.table("user_course_progress")
            .select("id, completed")
            .eq("user_id", user_id)
            .eq("chapter_id", chapter_id)
            .maybe_single()
            .execute()
        )
        already_completed = bool(existing_resp.data and existing_resp.data.get("completed"))
    except Exception as exc:
        logger.warning("[classroom.complete_chapter] existing progress lookup FAILED [%s] %s", type(exc).__name__, supabase_error(exc))
        already_completed = False

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    try:
        supabase.table("user_course_progress").upsert(
            {
                "user_id": user_id,
                "course_id": course_id,
                "chapter_id": chapter_id,
                "completed": True,
                "completed_at": now,
            },
            on_conflict="user_id,chapter_id",
        ).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.complete_chapter] upsert FAILED course_id=%s chapter_id=%s [%s] %s", course_id, chapter_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al marcar capítulo completado: {msg}")

    try:
        total_resp = supabase.table("course_chapters").select("id", count="exact").eq("course_id", course_id).execute()
        total = total_resp.count or 0
    except Exception as exc:
        logger.warning("[classroom.complete_chapter] total chapters count FAILED [%s] %s", type(exc).__name__, supabase_error(exc))
        total = 0

    try:
        done_resp = (
            supabase.table("user_course_progress")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("course_id", course_id)
            .eq("completed", True)
            .execute()
        )
        completed_count = done_resp.count or 0
    except Exception as exc:
        logger.warning("[classroom.complete_chapter] completed chapters count FAILED [%s] %s", type(exc).__name__, supabase_error(exc))
        completed_count = 0

    course_completed = total > 0 and completed_count >= total

    if not already_completed:
        _award(user_id, "lesson_completed", {"course_id": course_id, "chapter_id": chapter_id})

    if course_completed:
        _award(user_id, "course_completed", {"course_id": course_id})

    logger.info("[classroom.complete_chapter] OK course_id=%s chapter_id=%s completed=%d/%d", course_id, chapter_id, completed_count, total)
    return {
        "completed": True,
        "completedChapters": completed_count,
        "totalChapters": total,
        "progress": _progress_pct(completed_count, total),
        "courseCompleted": course_completed,
    }


def get_course_progress(course_id: str, user_id: str) -> dict:
    """
    Returns: { courseId, completedChapters, totalChapters, progress }
    Raises:
        HTTPException 404 — curso no encontrado
        HTTPException 500
    """
    logger.info("[classroom.get_course_progress] course_id=%s user_id=%s", course_id, user_id)
    supabase = get_supabase()
    _get_course_or_404(supabase, course_id)

    try:
        total_resp = supabase.table("course_chapters").select("id", count="exact").eq("course_id", course_id).execute()
        total = total_resp.count or 0
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.get_course_progress] total FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener progreso: {msg}")

    try:
        done_resp = (
            supabase.table("user_course_progress")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("course_id", course_id)
            .eq("completed", True)
            .execute()
        )
        completed = done_resp.count or 0
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.get_course_progress] completed FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener progreso: {msg}")

    return {
        "courseId": course_id,
        "completedChapters": completed,
        "totalChapters": total,
        "progress": _progress_pct(completed, total),
    }


# ---------------------------------------------------------------------------
# Servicios públicos (admin)
# ---------------------------------------------------------------------------

def admin_create_course(title: str, description: str, thumbnail: Optional[str], category: str, created_by: str) -> dict:
    """
    Raises:
        HTTPException 500
    """
    logger.info("[classroom.admin_create_course] title=%s created_by=%s", title, created_by)
    supabase = get_supabase()

    course_data = {
        "title": title,
        "description": description,
        "thumbnail": thumbnail,
        "category": category,
        "is_published": False,
        "created_by": created_by,
    }

    try:
        resp = supabase.table("courses").insert(course_data).execute()
        course = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_create_course] insert FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear curso: {msg}")

    logger.info("[classroom.admin_create_course] OK course_id=%s", course.get("id"))
    return _map_course(course)


def admin_update_course(course_id: str, title: Optional[str], description: Optional[str], thumbnail: Optional[str], category: Optional[str]) -> dict:
    """
    Raises:
        HTTPException 404/500
    """
    logger.info("[classroom.admin_update_course] course_id=%s", course_id)
    supabase = get_supabase()
    _get_course_or_404(supabase, course_id)

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
            resp = supabase.table("courses").update(updates).eq("id", course_id).execute()
            course = resp.data[0]
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[classroom.admin_update_course] update FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al actualizar curso: {msg}")
    else:
        course = _get_course_or_404(supabase, course_id)

    logger.info("[classroom.admin_update_course] OK course_id=%s", course_id)
    return _map_course(course)


def admin_publish_course(course_id: str, is_published: bool) -> dict:
    """
    Raises:
        HTTPException 404/500
    """
    logger.info("[classroom.admin_publish_course] course_id=%s is_published=%s", course_id, is_published)
    supabase = get_supabase()
    _get_course_or_404(supabase, course_id)

    try:
        resp = supabase.table("courses").update({"is_published": is_published}).eq("id", course_id).execute()
        course = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_publish_course] FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al publicar curso: {msg}")

    logger.info("[classroom.admin_publish_course] OK course_id=%s is_published=%s", course_id, is_published)
    return _map_course(course)


def admin_delete_course(course_id: str) -> dict:
    """
    Elimina un curso junto con sus capítulos y el progreso de los usuarios.

    Raises:
        HTTPException 404/500
    """
    logger.info("[classroom.admin_delete_course] course_id=%s", course_id)
    supabase = get_supabase()
    _get_course_or_404(supabase, course_id)

    try:
        supabase.table("user_course_progress").delete().eq("course_id", course_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_delete_course] progress delete FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar progreso del curso: {msg}")

    try:
        supabase.table("course_chapters").delete().eq("course_id", course_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_delete_course] chapters delete FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar capítulos del curso: {msg}")

    try:
        supabase.table("courses").delete().eq("id", course_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_delete_course] course delete FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar curso: {msg}")

    logger.info("[classroom.admin_delete_course] OK course_id=%s", course_id)
    return {"deleted": True}


def admin_create_chapter(course_id: str, title: str, description: Optional[str], video_url: Optional[str], duration: Optional[int]) -> dict:
    """
    Raises:
        HTTPException 404/500
    """
    logger.info("[classroom.admin_create_chapter] course_id=%s title=%s", course_id, title)
    supabase = get_supabase()
    _get_course_or_404(supabase, course_id)

    try:
        existing = supabase.table("course_chapters").select("id").eq("course_id", course_id).execute()
        sort_order = len(existing.data) if existing.data else 0
    except Exception as exc:
        logger.warning("[classroom.admin_create_chapter] sort_order fetch FAILED course_id=%s, defaulting to 0 [%s]", course_id, supabase_error(exc))
        sort_order = 0

    try:
        resp = supabase.table("course_chapters").insert({
            "course_id": course_id,
            "title": title,
            "description": description,
            "video_url": video_url,
            "duration": duration,
            "sort_order": sort_order,
        }).execute()
        chapter = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_create_chapter] insert FAILED course_id=%s [%s] %s", course_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear capítulo: {msg}")

    logger.info("[classroom.admin_create_chapter] OK chapter_id=%s", chapter.get("id"))
    return _map_chapter(chapter)


def admin_update_chapter(course_id: str, chapter_id: str, title: Optional[str], description: Optional[str], video_url: Optional[str], duration: Optional[int]) -> dict:
    """
    Raises:
        HTTPException 404/500
    """
    logger.info("[classroom.admin_update_chapter] course_id=%s chapter_id=%s", course_id, chapter_id)
    supabase = get_supabase()

    updates: dict = {}
    if title is not None:
        updates["title"] = title
    if description is not None:
        updates["description"] = description
    if video_url is not None:
        updates["video_url"] = video_url
    if duration is not None:
        updates["duration"] = duration

    if updates:
        try:
            resp = (
                supabase.table("course_chapters")
                .update(updates)
                .eq("id", chapter_id)
                .eq("course_id", course_id)
                .execute()
            )
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[classroom.admin_update_chapter] update FAILED chapter_id=%s [%s] %s", chapter_id, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al actualizar capítulo: {msg}")
        if not resp.data:
            raise HTTPException(status_code=404, detail="Capítulo no encontrado")
        chapter = resp.data[0]
    else:
        try:
            resp = (
                supabase.table("course_chapters")
                .select("*")
                .eq("id", chapter_id)
                .eq("course_id", course_id)
                .maybe_single()
                .execute()
            )
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[classroom.admin_update_chapter] fetch FAILED chapter_id=%s [%s] %s", chapter_id, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al obtener capítulo: {msg}")
        if not resp.data:
            raise HTTPException(status_code=404, detail="Capítulo no encontrado")
        chapter = resp.data

    logger.info("[classroom.admin_update_chapter] OK chapter_id=%s", chapter_id)
    return _map_chapter(chapter)


def admin_delete_chapter(course_id: str, chapter_id: str) -> dict:
    """
    Raises:
        HTTPException 404/500
    """
    logger.info("[classroom.admin_delete_chapter] course_id=%s chapter_id=%s", course_id, chapter_id)
    supabase = get_supabase()

    try:
        existing = (
            supabase.table("course_chapters")
            .select("id")
            .eq("id", chapter_id)
            .eq("course_id", course_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_delete_chapter] lookup FAILED chapter_id=%s [%s] %s", chapter_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al buscar capítulo: {msg}")

    if not existing.data:
        raise HTTPException(status_code=404, detail="Capítulo no encontrado")

    try:
        supabase.table("user_course_progress").delete().eq("chapter_id", chapter_id).execute()
    except Exception as exc:
        logger.warning("[classroom.admin_delete_chapter] progress cleanup skipped chapter_id=%s [%s] %s", chapter_id, type(exc).__name__, supabase_error(exc))

    try:
        supabase.table("course_chapters").delete().eq("id", chapter_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_delete_chapter] delete FAILED chapter_id=%s [%s] %s", chapter_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar capítulo: {msg}")

    logger.info("[classroom.admin_delete_chapter] OK chapter_id=%s", chapter_id)
    return {"deleted": True}

# ---------------------------------------------------------------------------
# Chapter PDFs
# ---------------------------------------------------------------------------

def _map_pdf(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "chapterId": c.get("chapter_id"),
        "title": c.get("title"),
        "fileUrl": c.get("file_url"),
        "sortOrder": c.get("sort_order"),
        "createdAt": c.get("created_at"),
    }


def get_chapter_pdfs(chapter_id: str) -> list:
    logger.info("[classroom.get_chapter_pdfs] chapter_id=%s", chapter_id)
    supabase = get_supabase()
    try:
        resp = supabase.table("chapter_pdfs").select("*").eq("chapter_id", chapter_id).order("sort_order").execute()
        return [_map_pdf(p) for p in (resp.data or [])]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.get_chapter_pdfs] FAILED chapter_id=%s [%s] %s", chapter_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener PDFs: {msg}")


def admin_upload_chapter_pdf(chapter_id: str, title: str, file_data: str, filename: str) -> dict:
    import base64
    import re
    import time
    from app.core.supabase import is_supabase_configured

    logger.info("[classroom.admin_upload_chapter_pdf] chapter_id=%s title=%s filename=%s len=%d", chapter_id, title, filename, len(file_data))

    if not is_supabase_configured():
        raise HTTPException(status_code=500, detail="Supabase no configurado, no se pueden subir documentos.")

    match = re.match(r"^data:(.+);base64,(.+)$", file_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de archivo inválido")

    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    supabase = get_supabase()

    ext = "pdf"
    if "." in filename:
        ext = filename.split(".")[-1]
    
    # Bucket is chapter-pdfs, path is {chapter_id}/{filename} (with timestamp to avoid collision)
    path = f"{chapter_id}/{int(time.time() * 1000)}_{filename}"

    try:
        supabase.storage.from_("chapter-pdfs").upload(
            path, raw_bytes,
            file_options={"content-type": mime_type, "upsert": "false"},
        )
        url = supabase.storage.from_("chapter-pdfs").get_public_url(path)
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_upload_chapter_pdf] upload FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al subir documento: {msg}")

    try:
        existing = supabase.table("chapter_pdfs").select("id").eq("chapter_id", chapter_id).execute()
        sort_order = len(existing.data) if existing.data else 0
    except Exception:
        sort_order = 0

    try:
        resp = supabase.table("chapter_pdfs").insert({
            "chapter_id": chapter_id,
            "title": title,
            "file_url": url,
            "sort_order": sort_order,
        }).execute()
        pdf_record = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_upload_chapter_pdf] insert FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear registro de PDF: {msg}")

    return _map_pdf(pdf_record)


def admin_update_chapter_pdf(chapter_id: str, pdf_id: str, title: Optional[str], sort_order: Optional[int]) -> dict:
    logger.info("[classroom.admin_update_chapter_pdf] chapter_id=%s pdf_id=%s", chapter_id, pdf_id)
    supabase = get_supabase()

    updates = {}
    if title is not None:
        updates["title"] = title
    if sort_order is not None:
        updates["sort_order"] = sort_order

    if updates:
        try:
            resp = supabase.table("chapter_pdfs").update(updates).eq("id", pdf_id).eq("chapter_id", chapter_id).execute()
            if not resp.data:
                raise HTTPException(status_code=404, detail="PDF no encontrado")
            return _map_pdf(resp.data[0])
        except HTTPException:
            raise
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[classroom.admin_update_chapter_pdf] update FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al actualizar PDF: {msg}")
    
    # if no updates
    try:
        resp = supabase.table("chapter_pdfs").select("*").eq("id", pdf_id).eq("chapter_id", chapter_id).maybe_single().execute()
        if not resp.data:
            raise HTTPException(status_code=404, detail="PDF no encontrado")
        return _map_pdf(resp.data)
    except HTTPException:
        raise
    except Exception as exc:
        msg = supabase_error(exc)
        raise HTTPException(status_code=500, detail=f"Error al obtener PDF: {msg}")


def admin_delete_chapter_pdf(chapter_id: str, pdf_id: str) -> dict:
    logger.info("[classroom.admin_delete_chapter_pdf] chapter_id=%s pdf_id=%s", chapter_id, pdf_id)
    supabase = get_supabase()

    try:
        existing = supabase.table("chapter_pdfs").select("*").eq("id", pdf_id).eq("chapter_id", chapter_id).maybe_single().execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="PDF no encontrado")
        file_url = existing.data.get("file_url")
    except HTTPException:
        raise
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_delete_chapter_pdf] lookup FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al buscar PDF: {msg}")

    # attempt to delete from bucket if possible
    try:
        if file_url and "chapter-pdfs/" in file_url:
            path = file_url.split("chapter-pdfs/")[-1]
            supabase.storage.from_("chapter-pdfs").remove([path])
    except Exception as exc:
        logger.warning("[classroom.admin_delete_chapter_pdf] bucket remove FAILED [%s]", exc)

    try:
        supabase.table("chapter_pdfs").delete().eq("id", pdf_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[classroom.admin_delete_chapter_pdf] delete FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar PDF: {msg}")

    return {"deleted": True}

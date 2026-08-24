import base64
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from app.core.cache import cache_delete, cache_get, cache_set
from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

_LIVES_KEY = "lives:all"
_LIVES_TTL = 5  # segundos — ventana corta para que el inicio/fin de un live sea visible rápido

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

def _map_live(l: dict) -> dict:
    return {
        "id": l.get("id"),
        "title": l.get("title"),
        "description": l.get("description"),
        "youtubeUrl": l.get("youtube_url"),
        "isActive": l.get("is_active"),
        "scheduledAt": l.get("scheduled_at"),
        "endedAt": l.get("ended_at"),
        "createdBy": l.get("created_by"),
        "createdAt": l.get("created_at"),
    }


def _map_chat_message(m: dict) -> dict:
    profile = m.get("profiles") or {}
    return {
        "id": m.get("id"),
        "liveId": m.get("live_id"),
        "userId": m.get("user_id"),
        "content": m.get("content"),
        "createdAt": m.get("created_at"),
        "editedAt": m.get("edited_at"),
        "isPinned": m.get("is_pinned", False),
        "author": {
            "name": profile.get("name"),
            "avatar": profile.get("avatar"),
            "role": profile.get("role"),
        },
    }


def _map_pdf(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "liveId": p.get("live_id"),
        "title": p.get("title"),
        "fileUrl": p.get("file_url"),
        "sortOrder": p.get("sort_order"),
        "createdAt": p.get("created_at"),
    }


def _get_live_or_404(supabase, live_id: str) -> dict:
    try:
        resp = supabase.table("live_sessions").select("*").eq("id", live_id).maybe_single().execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives._get_live_or_404] FAILED live_id=%s [%s] %s", live_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al buscar transmisión: {msg}")

    if not resp.data:
        raise HTTPException(status_code=404, detail="Transmisión no encontrada")

    return resp.data


def _fetch_profile(supabase, user_id: str) -> dict:
    try:
        resp = supabase.table("profiles").select("name, avatar, role").eq("id", user_id).single().execute()
        return resp.data or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Servicios miembro — sesiones
# ---------------------------------------------------------------------------

def get_lives() -> list:
    cached = cache_get(_LIVES_KEY)
    if cached is not None:
        return cached

    logger.info("[lives.get_lives]")
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("live_sessions")
            .select("*")
            .order("is_active", desc=True)
            .order("scheduled_at")
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.get_lives] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener transmisiones: {msg}")
    result = [_map_live(l) for l in (resp.data or [])]
    cache_set(_LIVES_KEY, result, _LIVES_TTL)
    return result


def get_active_live() -> Optional[dict]:
    logger.info("[lives.get_active_live]")
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("live_sessions")
            .select("*")
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.get_active_live] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener transmisión activa: {msg}")

    if not resp.data:
        return None
    return _map_live(resp.data[0])


# ---------------------------------------------------------------------------
# Servicios miembro — chat
# ---------------------------------------------------------------------------

def get_chat_messages(live_id: str, limit: int, after: Optional[str]) -> list:
    logger.info("[lives.get_chat_messages] live_id=%s limit=%s after=%s", live_id, limit, after)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    query = (
        supabase.table("live_chat_messages")
        .select("id, live_id, user_id, content, created_at, edited_at, is_pinned, profiles(name, avatar, role)")
        .eq("live_id", live_id)
    )

    try:
        if after:
            resp = query.gt("created_at", after).order("created_at").limit(limit).execute()
            rows = resp.data or []
        else:
            resp = query.order("created_at", desc=True).limit(limit).execute()
            rows = list(reversed(resp.data or []))
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.get_chat_messages] FAILED live_id=%s [%s] %s", live_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener mensajes: {msg}")

    return [_map_chat_message(m) for m in rows]


def send_chat_message(live_id: str, user_id: str, content: str) -> dict:
    logger.info("[lives.send_chat_message] live_id=%s user_id=%s", live_id, user_id)
    supabase = get_supabase()
    live = _get_live_or_404(supabase, live_id)

    if not live.get("is_active"):
        raise HTTPException(status_code=400, detail="La transmisión en vivo no está activa")

    try:
        resp = supabase.table("live_chat_messages").insert({
            "live_id": live_id, "user_id": user_id, "content": content,
        }).execute()
        message = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.send_chat_message] insert FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al enviar mensaje: {msg}")

    profile = _fetch_profile(supabase, user_id)
    logger.info("[lives.send_chat_message] OK live_id=%s message_id=%s", live_id, message.get("id"))
    return _map_chat_message({**message, "profiles": profile})


# ---------------------------------------------------------------------------
# Servicios miembro — reacciones
# ---------------------------------------------------------------------------

def get_live_reactions(live_id: str, user_id: str) -> dict:
    logger.info("[lives.get_live_reactions] live_id=%s", live_id)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)
    try:
        resp = supabase.table("live_reactions").select("reaction_type, user_id").eq("live_id", live_id).execute()
        reactions: dict = {}
        user_reaction = None
        for r in (resp.data or []):
            rt = r["reaction_type"]
            reactions[rt] = reactions.get(rt, 0) + 1
            if r["user_id"] == user_id:
                user_reaction = rt
        return {"reactions": reactions, "userReaction": user_reaction}
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.get_live_reactions] FAILED live_id=%s [%s] %s", live_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener reacciones: {msg}")


def react_live(live_id: str, user_id: str, reaction_type: str) -> dict:
    logger.info("[lives.react_live] live_id=%s user_id=%s reaction=%s", live_id, user_id, reaction_type)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    try:
        resp = supabase.table("live_reactions").select("id, reaction_type").eq("live_id", live_id).eq("user_id", user_id).maybe_single().execute()
        existing = (resp.data if resp is not None else None)
    except Exception as exc:
        msg = supabase_error(exc)
        raise HTTPException(status_code=500, detail=f"Error: {msg}")

    try:
        if existing:
            if existing["reaction_type"] == reaction_type:
                supabase.table("live_reactions").delete().eq("id", existing["id"]).execute()
            else:
                supabase.table("live_reactions").update({"reaction_type": reaction_type}).eq("id", existing["id"]).execute()
        else:
            supabase.table("live_reactions").insert({"live_id": live_id, "user_id": user_id, "reaction_type": reaction_type}).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.react_live] mutation FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al reaccionar: {msg}")

    return get_live_reactions(live_id, user_id)


# ---------------------------------------------------------------------------
# Servicios miembro — PDFs
# ---------------------------------------------------------------------------

def get_live_pdfs(live_id: str) -> list:
    logger.info("[lives.get_live_pdfs] live_id=%s", live_id)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)
    try:
        resp = supabase.table("live_pdfs").select("*").eq("live_id", live_id).order("sort_order").execute()
        return [_map_pdf(p) for p in (resp.data or [])]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.get_live_pdfs] FAILED live_id=%s [%s] %s", live_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener PDFs: {msg}")


# ---------------------------------------------------------------------------
# Servicios admin — sesiones
# ---------------------------------------------------------------------------

def admin_create_live(title: str, youtube_url: Optional[str], description: Optional[str],
                      scheduled_at: Optional[datetime], created_by: str) -> dict:
    logger.info("[lives.admin_create_live] title=%s created_by=%s", title, created_by)
    supabase = get_supabase()

    data: dict = {"title": title, "is_active": False, "created_by": created_by}
    if youtube_url is not None:
        data["youtube_url"] = youtube_url
    if description is not None:
        data["description"] = description
    if scheduled_at is not None:
        data["scheduled_at"] = scheduled_at.isoformat()

    try:
        resp = supabase.table("live_sessions").insert(data).execute()
        live = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.admin_create_live] insert FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear transmisión: {msg}")

    cache_delete(_LIVES_KEY)
    logger.info("[lives.admin_create_live] OK live_id=%s", live.get("id"))
    return _map_live(live)


def admin_update_live(live_id: str, title: Optional[str], youtube_url: Optional[str],
                      description: Optional[str], scheduled_at: Optional[datetime]) -> dict:
    logger.info("[lives.admin_update_live] live_id=%s", live_id)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    updates: dict = {}
    if title is not None:
        updates["title"] = title
    if youtube_url is not None:
        updates["youtube_url"] = youtube_url
    if description is not None:
        updates["description"] = description
    if scheduled_at is not None:
        updates["scheduled_at"] = scheduled_at.isoformat()

    if updates:
        try:
            resp = supabase.table("live_sessions").update(updates).eq("id", live_id).execute()
            live = resp.data[0]
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[lives.admin_update_live] FAILED live_id=%s [%s] %s", live_id, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al actualizar transmisión: {msg}")
    else:
        live = _get_live_or_404(supabase, live_id)

    cache_delete(_LIVES_KEY)
    logger.info("[lives.admin_update_live] OK live_id=%s", live_id)
    return _map_live(live)


def admin_set_active(live_id: str, is_active: bool) -> dict:
    logger.info("[lives.admin_set_active] live_id=%s is_active=%s", live_id, is_active)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    now = datetime.now(timezone.utc).isoformat()

    if is_active:
        try:
            supabase.table("live_sessions").update({"is_active": False, "ended_at": now}).eq("is_active", True).neq("id", live_id).execute()
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[lives.admin_set_active] deactivate-others FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al desactivar otras transmisiones: {msg}")
        updates = {"is_active": True, "ended_at": None}
    else:
        updates = {"is_active": False, "ended_at": now}

    try:
        resp = supabase.table("live_sessions").update(updates).eq("id", live_id).execute()
        live = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.admin_set_active] FAILED live_id=%s [%s] %s", live_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al actualizar transmisión: {msg}")

    cache_delete(_LIVES_KEY)
    logger.info("[lives.admin_set_active] OK live_id=%s", live_id)
    return _map_live(live)


def admin_delete_live(live_id: str) -> dict:
    logger.info("[lives.admin_delete_live] live_id=%s", live_id)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    # Las filas hijas se borran explícitamente: si alguna FK no tiene ON DELETE
    # CASCADE, el DELETE de live_sessions falla por violación de clave foránea.
    for table, label in (
        ("live_reactions", "reacciones"),
        ("live_pdfs", "PDFs"),
        ("live_chat_messages", "mensajes"),
    ):
        try:
            supabase.table(table).delete().eq("live_id", live_id).execute()
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[lives.admin_delete_live] %s delete FAILED [%s] %s", table, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al eliminar {label}: {msg}")

    # Limpieza best-effort del bucket: un fallo aquí deja archivos huérfanos,
    # pero no debe impedir que se borre la transmisión.
    try:
        objects = supabase.storage.from_("live-pdfs").list(live_id) or []
        paths = [f"{live_id}/{o['name']}" for o in objects if o.get("name")]
        if paths:
            supabase.storage.from_("live-pdfs").remove(paths)
    except Exception as exc:
        logger.warning("[lives.admin_delete_live] storage cleanup fallido live_id=%s [%s] %s",
                       live_id, type(exc).__name__, exc)

    try:
        supabase.table("live_sessions").delete().eq("id", live_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.admin_delete_live] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar transmisión: {msg}")

    cache_delete(_LIVES_KEY)
    logger.info("[lives.admin_delete_live] OK live_id=%s", live_id)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Servicios admin — PDFs
# ---------------------------------------------------------------------------

def admin_add_pdf(live_id: str, title: str, file_data: str, filename: str) -> dict:
    logger.info("[lives.admin_add_pdf] live_id=%s filename=%s", live_id, filename)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    match = re.match(r"^data:(.+);base64,(.+)$", file_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de archivo inválido")

    mime_type = match.group(1)
    file_bytes = base64.b64decode(match.group(2))
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    path = f"{live_id}/{safe_name}"

    try:
        supabase.storage.from_("live-pdfs").upload(path, file_bytes, {"content-type": mime_type, "upsert": "true"})
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.admin_add_pdf] upload FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al subir PDF: {msg}")

    try:
        url_resp = supabase.storage.from_("live-pdfs").get_public_url(path)
        file_url = url_resp if isinstance(url_resp, str) else str(url_resp)
    except Exception:
        file_url = path

    try:
        count_resp = supabase.table("live_pdfs").select("id", count="exact").eq("live_id", live_id).execute()
        sort_order = count_resp.count or 0
        resp = supabase.table("live_pdfs").insert({
            "live_id": live_id, "title": title, "file_url": file_url, "sort_order": sort_order,
        }).execute()
        logger.info("[lives.admin_add_pdf] OK pdf_id=%s", resp.data[0].get("id"))
        return _map_pdf(resp.data[0])
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.admin_add_pdf] insert FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al guardar PDF: {msg}")


def admin_delete_pdf(live_id: str, pdf_id: str) -> dict:
    logger.info("[lives.admin_delete_pdf] pdf_id=%s", pdf_id)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    try:
        supabase.table("live_pdfs").delete().eq("id", pdf_id).eq("live_id", live_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.admin_delete_pdf] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar PDF: {msg}")

    logger.info("[lives.admin_delete_pdf] OK pdf_id=%s", pdf_id)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Servicios admin — chat
# ---------------------------------------------------------------------------

def admin_edit_message(live_id: str, message_id: str, content: str, admin_id: str) -> dict:
    logger.info("[lives.admin_edit_message] msg_id=%s admin=%s", message_id, admin_id)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    try:
        msg_resp = supabase.table("live_chat_messages").select("*").eq("id", message_id).eq("live_id", live_id).maybe_single().execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=supabase_error(exc))

    if not msg_resp.data:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")
    if msg_resp.data["user_id"] != admin_id:
        raise HTTPException(status_code=403, detail="Solo puedes editar tus propios mensajes")

    now = datetime.now(timezone.utc).isoformat()
    try:
        resp = supabase.table("live_chat_messages").update({"content": content, "edited_at": now}).eq("id", message_id).execute()
        message = resp.data[0]
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.admin_edit_message] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al editar mensaje: {msg}")

    profile = _fetch_profile(supabase, message.get("user_id", ""))
    logger.info("[lives.admin_edit_message] OK msg_id=%s", message_id)
    return _map_chat_message({**message, "profiles": profile})


def admin_delete_message(live_id: str, message_id: str) -> dict:
    logger.info("[lives.admin_delete_message] msg_id=%s", message_id)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    try:
        supabase.table("live_chat_messages").delete().eq("id", message_id).eq("live_id", live_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.admin_delete_message] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar mensaje: {msg}")

    logger.info("[lives.admin_delete_message] OK msg_id=%s", message_id)
    return {"deleted": True, "id": message_id}


def admin_pin_message(live_id: str, message_id: str, is_pinned: bool) -> dict:
    logger.info("[lives.admin_pin_message] msg_id=%s is_pinned=%s", message_id, is_pinned)
    supabase = get_supabase()
    _get_live_or_404(supabase, live_id)

    try:
        if is_pinned:
            supabase.table("live_chat_messages").update({"is_pinned": False}).eq("live_id", live_id).eq("is_pinned", True).neq("id", message_id).execute()
        resp = supabase.table("live_chat_messages").update({"is_pinned": is_pinned}).eq("id", message_id).eq("live_id", live_id).execute()
        if not resp.data:
            raise HTTPException(status_code=404, detail="Mensaje no encontrado")
        message = resp.data[0]
    except HTTPException:
        raise
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[lives.admin_pin_message] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al destacar mensaje: {msg}")

    profile = _fetch_profile(supabase, message.get("user_id", ""))
    logger.info("[lives.admin_pin_message] OK msg_id=%s", message_id)
    return _map_chat_message({**message, "profiles": profile})

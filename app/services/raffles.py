import base64
import logging
import random
import re
from datetime import datetime, timedelta, timezone

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


def upload_raffle_image(image_data: str) -> str:
    match = re.match(r"^data:(.+);base64,(.+)$", image_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de imagen inválido")
    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    ext = _ext_from_mime(mime_type)
    path = f"raffle-{datetime.now(timezone.utc).timestamp()}.{ext}"
    supabase = get_supabase()
    try:
        supabase.storage.from_("raffle-images").upload(
            path, raw_bytes,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles.upload_image] FAILED path=%s [%s] %s", path, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error subiendo imagen: {msg}")
    return supabase.storage.from_("raffle-images").get_public_url(path)


def _get_email_map() -> dict:
    """Un solo llamado a la Auth admin API para resolver user_id -> email (evita N llamadas por ganador)."""
    supabase = get_supabase()
    try:
        users_resp = supabase.auth.admin.list_users()
        return {u.id: u.email for u in (users_resp or [])}
    except Exception as exc:
        logger.warning("[raffles._get_email_map] FAILED [%s]", supabase_error(exc))
        return {}


def _map_winner(w: dict, email_map: dict | None = None) -> dict:
    profile = w.get("profiles") or {}
    winner = {
        "id": w["id"],
        "user_id": w["user_id"],
        "position": w["position"],
        "name": profile.get("name") or "Sin nombre",
        "avatar": profile.get("avatar") or None,
    }
    if email_map is not None:
        winner["email"] = email_map.get(w["user_id"])
    return winner


def _map_raffle(r: dict, email_map: dict | None = None) -> dict:
    raw_winners = r.get("raffle_winners") or []
    winners = sorted([_map_winner(w, email_map) for w in raw_winners], key=lambda w: w["position"])
    return {
        "id": r["id"],
        "title": r["title"],
        "description": r.get("description"),
        "image_url": r.get("image_url"),
        "winner_count": r["winner_count"],
        "draw_at": r.get("draw_at"),
        "drawn_at": r.get("drawn_at"),
        "is_active": r.get("drawn_at") is None,
        "created_at": r["created_at"],
        "winners": winners,
    }


def create_raffle(title: str, description: str, image_url: str, winner_count: int, draw_at: str, created_by: str) -> dict:
    logger.info("[raffles.create] title=%s winners=%d draw_at=%s by=%s", title, winner_count, draw_at, created_by)
    supabase = get_supabase()

    if _get_pending_raffle() is not None:
        raise HTTPException(
            status_code=400,
            detail="Ya hay un sorteo activo. Espera a que se sortee o elimínalo antes de programar uno nuevo.",
        )

    try:
        raffle_resp = (
            supabase.table("raffles")
            .insert({
                "title": title,
                "description": description,
                "image_url": image_url,
                "winner_count": winner_count,
                "draw_at": draw_at,
                "created_by": created_by,
            })
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles.create] FAILED inserting raffle: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    return get_raffle(raffle_resp.data[0]["id"], include_email=True)


def draw_raffle(raffle_id: str, include_email: bool = False) -> dict:
    """
    Elige ganadores al azar entre miembros activos y marca el sorteo como sorteado.
    Raises: HTTPException 400 (ya sorteado / no hay suficientes elegibles), 404, 500
    """
    logger.info("[raffles.draw] raffle_id=%s", raffle_id)
    supabase = get_supabase()

    raffle = get_raffle(raffle_id)
    if not raffle["is_active"]:
        raise HTTPException(status_code=400, detail="Este sorteo ya fue realizado.")

    try:
        eligible_resp = (
            supabase.table("profiles")
            .select("id")
            .eq("subscription_status", "active")
            .eq("role", "miembro")
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles.draw] FAILED fetching eligible members: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    eligible = eligible_resp.data or []
    winner_count = raffle["winner_count"]
    if len(eligible) < winner_count:
        raise HTTPException(
            status_code=400,
            detail=f"Solo hay {len(eligible)} miembro(s) activo(s). No se puede sortear {winner_count} ganador(es).",
        )

    selected = random.sample(eligible, winner_count)
    winner_rows = [
        {"raffle_id": raffle_id, "user_id": s["id"], "position": i + 1}
        for i, s in enumerate(selected)
    ]
    try:
        supabase.table("raffle_winners").insert(winner_rows).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles.draw] FAILED inserting winners: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    try:
        supabase.table("raffles").update({
            "drawn_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", raffle_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles.draw] FAILED setting drawn_at: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    result = get_raffle(raffle_id, include_email=True)
    _notify_winners(result["title"], result["winners"])
    if not include_email:
        for w in result["winners"]:
            w.pop("email", None)
    return result


def _notify_winners(raffle_title: str, winners: list) -> None:
    """Envía el correo de felicitación a cada ganador. Fire-and-forget, nunca bloquea el draw."""
    from app.services import email as email_service

    for w in winners:
        to = w.get("email")
        if not to:
            logger.warning("[raffles._notify_winners] sin email para user_id=%s, se omite notificación", w.get("user_id"))
            continue
        email_service.send_raffle_winner(to, w.get("name") or "miembro", raffle_title)


def draw_scheduled_raffles() -> dict:
    """
    Sortea automáticamente todos los sorteos cuya draw_at ya pasó y siguen pendientes.
    Llamado por el cron de Supabase (pg_cron). Un fallo en un sorteo no bloquea el resto.
    """
    logger.info("[raffles.draw_scheduled] checking due raffles")
    supabase = get_supabase()

    try:
        due_resp = (
            supabase.table("raffles")
            .select("id")
            .is_("drawn_at", "null")
            .lte("draw_at", datetime.now(timezone.utc).isoformat())
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles.draw_scheduled] FAILED fetching due raffles: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    due = due_resp.data or []
    drawn, errors = [], []
    for row in due:
        raffle_id = row["id"]
        try:
            draw_raffle(raffle_id)
            drawn.append(raffle_id)
        except Exception as exc:
            logger.error("[raffles.draw_scheduled] FAILED drawing raffle_id=%s [%s]", raffle_id, exc)
            errors.append({"raffle_id": raffle_id, "error": str(exc)})

    logger.info("[raffles.draw_scheduled] drawn=%d errors=%d", len(drawn), len(errors))
    return {"drawn": drawn, "errors": errors}


def _get_pending_raffle() -> dict | None:
    """El sorteo pendiente (sin sortear), si existe. Solo puede haber uno a la vez."""
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("raffles")
            .select("*, raffle_winners(id, user_id, position, profiles(name, avatar))")
            .is_("drawn_at", "null")
            .order("draw_at", desc=False)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles._get_pending] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = resp.data or []
    if not rows:
        return None
    return _map_raffle(rows[0])


_WINNERS_VISIBLE_FOR = timedelta(days=1)


def get_active_raffle() -> dict | None:
    """
    El sorteo "actual" para la Comunidad: el más reciente, pendiente o ya sorteado.
    Mientras está pendiente se muestra la cuenta regresiva; una vez sorteado, los
    ganadores por _WINNERS_VISIBLE_FOR (luego el banner deja de mostrarse aunque
    no haya un sorteo nuevo programado). No incluye email (dato privado, solo admin).
    """
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("raffles")
            .select("*, raffle_winners(id, user_id, position, profiles(name, avatar))")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles.get_active] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = resp.data or []
    if not rows:
        return None

    raffle = rows[0]
    drawn_at = raffle.get("drawn_at")
    if drawn_at is not None:
        drawn_dt = datetime.fromisoformat(drawn_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - drawn_dt > _WINNERS_VISIBLE_FOR:
            return None

    return _map_raffle(raffle)


def get_raffle(raffle_id: str, include_email: bool = False) -> dict:
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("raffles")
            .select("*, raffle_winners(id, user_id, position, profiles(name, avatar))")
            .eq("id", raffle_id)
            .single()
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        raise HTTPException(status_code=500, detail=msg)
    email_map = _get_email_map() if include_email else None
    return _map_raffle(resp.data, email_map)


def list_raffles(include_email: bool = False) -> list:
    logger.info("[raffles.list] fetching")
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("raffles")
            .select("*, raffle_winners(id, user_id, position, profiles(name, avatar))")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles.list] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    email_map = _get_email_map() if include_email else None
    return [_map_raffle(r, email_map) for r in (resp.data or [])]


def delete_raffle(raffle_id: str) -> dict:
    logger.info("[raffles.delete] raffle_id=%s", raffle_id)
    supabase = get_supabase()
    try:
        supabase.table("raffles").delete().eq("id", raffle_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[raffles.delete] FAILED: %s", msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return {"deleted": True}

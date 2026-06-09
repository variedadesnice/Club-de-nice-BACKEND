import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)


def _get_tier_for_level(supabase, level: int) -> Optional[dict]:
    try:
        resp = (
            supabase.table("level_tiers")
            .select("id, name, min_level, max_level, description, icon_url")
            .lte("min_level", level)
            .gte("max_level", level)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception:
        return None


# ── Public ────────────────────────────────────────────────────────────────────

def get_tiers() -> list:
    """
    Returns: Lista de level_tiers ordenados por min_level.
    Raises: HTTPException 500
    """
    logger.info("[levels.get_tiers]")
    supabase = get_supabase()
    try:
        resp = supabase.table("level_tiers").select("*").order("min_level").execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.get_tiers] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return resp.data or []


def get_user_level(user_id: str) -> dict:
    """
    Returns: { user_id, level, xp_total, xp_current, xp_next, tier? }
    Raises: HTTPException 500
    """
    logger.info("[levels.get_user_level] user_id=%s", user_id)
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("user_levels")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        data = resp.data[0] if resp.data else None
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.get_user_level] FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not data:
        data = {"user_id": user_id, "level": 1, "xp_total": 0, "xp_current": 0, "xp_next": 100}

    tier = _get_tier_for_level(supabase, data["level"])
    return {**data, "tier": tier}


def get_my_achievements(user_id: str) -> list:
    """
    Returns: Logros obtenidos por el usuario, con info del achievement_type.
    Raises: HTTPException 500
    """
    logger.info("[levels.get_my_achievements] user_id=%s", user_id)
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("user_achievements")
            .select("id, obtained_at, metadata, achievement_types(id, code, name, description, xp_reward, icon_url)")
            .eq("user_id", user_id)
            .order("obtained_at", desc=True)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.get_my_achievements] FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    result = []
    for row in (resp.data or []):
        atype = row.get("achievement_types") or {}
        result.append({
            "id": row["id"],
            "achievement_type_id": atype.get("id"),
            "code": atype.get("code"),
            "name": atype.get("name"),
            "description": atype.get("description"),
            "xp_reward": atype.get("xp_reward"),
            "icon_url": atype.get("icon_url"),
            "obtained_at": row["obtained_at"],
            "metadata": row.get("metadata"),
        })
    return result


def get_xp_history(user_id: str, limit: int = 20, offset: int = 0) -> dict:
    """
    Returns: { transactions: [...], limit, offset }
    Raises: HTTPException 500
    """
    logger.info("[levels.get_xp_history] user_id=%s limit=%s offset=%s", user_id, limit, offset)
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("xp_transactions")
            .select("id, amount, reason, achievement_type_id, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.get_xp_history] FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return {"transactions": resp.data or [], "limit": limit, "offset": offset}


def get_achievement_catalog() -> list:
    """
    Returns: Logros activos ordenados por xp_reward desc.
    Raises: HTTPException 500
    """
    logger.info("[levels.get_achievement_catalog]")
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("achievement_types")
            .select("id, code, name, description, xp_reward, is_repeatable, daily_limit, icon_url, is_active")
            .eq("is_active", True)
            .order("xp_reward", desc=True)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.get_achievement_catalog] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return resp.data or []


# ── Core algorithm ────────────────────────────────────────────────────────────

def process_achievement(user_id: str, achievement_code: str, metadata: Optional[dict] = None) -> dict:
    """
    Algoritmo central de gamificación. Otorga XP si se cumplen todas las condiciones.

    Returns:
        { xp_awarded, new_level, leveled_up, skipped }
        skipped=True cuando el logro se ignora silenciosamente (límite diario, ya obtenido).
    Raises:
        HTTPException 404 — achievement_code no existe o está inactivo
        HTTPException 500 — error de base de datos
    """
    logger.info("[levels.process_achievement] user_id=%s code=%s", user_id, achievement_code)
    supabase = get_supabase()

    # 1. Buscar achievement_type activo por code
    try:
        atype_resp = (
            supabase.table("achievement_types")
            .select("id, code, name, xp_reward, is_repeatable, daily_limit, is_active")
            .eq("code", achievement_code)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        atype = atype_resp.data[0] if atype_resp.data else None
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.process_achievement] atype fetch FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not atype:
        logger.warning("[levels.process_achievement] not found or inactive code=%s", achievement_code)
        raise HTTPException(status_code=404, detail=f"Logro '{achievement_code}' no encontrado o inactivo.")

    achievement_type_id = atype["id"]
    xp_reward = atype["xp_reward"]

    # 2. Logro no repetible → verificar que no lo tenga ya
    if not atype["is_repeatable"]:
        try:
            existing = (
                supabase.table("user_achievements")
                .select("id")
                .eq("user_id", user_id)
                .eq("achievement_type_id", achievement_type_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                logger.info("[levels.process_achievement] skipped (already earned) user_id=%s code=%s", user_id, achievement_code)
                return {"xp_awarded": 0, "new_level": 0, "leveled_up": False, "skipped": True}
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[levels.process_achievement] existing check FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=msg)

    # 3. Repetible con daily_limit → verificar límite diario (UTC)
    if atype["is_repeatable"] and atype["daily_limit"] is not None:
        today = datetime.now(timezone.utc).date()
        day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        try:
            count_resp = (
                supabase.table("xp_transactions")
                .select("id", count="exact")
                .eq("user_id", user_id)
                .eq("achievement_type_id", achievement_type_id)
                .gte("created_at", day_start.isoformat())
                .lt("created_at", day_end.isoformat())
                .execute()
            )
            count = count_resp.count or 0
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[levels.process_achievement] daily count FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=msg)

        if count >= atype["daily_limit"]:
            logger.info(
                "[levels.process_achievement] skipped (daily limit %s reached) user_id=%s code=%s",
                atype["daily_limit"], user_id, achievement_code,
            )
            return {"xp_awarded": 0, "new_level": 0, "leveled_up": False, "skipped": True}

    # 4. Insertar en user_achievements
    try:
        supabase.table("user_achievements").insert({
            "user_id": user_id,
            "achievement_type_id": achievement_type_id,
            "obtained_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata,
        }).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.process_achievement] insert user_achievement FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    # 5. Llamar award_xp RPC (registra xp_transaction + actualiza user_levels + sube nivel)
    try:
        rpc_resp = supabase.rpc("award_xp", {
            "p_user_id": user_id,
            "p_amount": xp_reward,
            "p_reason": atype["name"],
            "p_achievement_type_id": achievement_type_id,
        }).execute()
        rpc_data = rpc_resp.data or {}
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.process_achievement] award_xp RPC FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    leveled_up = bool(rpc_data.get("leveled_up", False))
    new_level = int(rpc_data.get("new_level", 0))
    logger.info(
        "[levels.process_achievement] OK user_id=%s code=%s xp=%s leveled_up=%s new_level=%s",
        user_id, achievement_code, xp_reward, leveled_up, new_level,
    )
    return {"xp_awarded": xp_reward, "new_level": new_level, "leveled_up": leveled_up, "skipped": False}


# ── Admin ─────────────────────────────────────────────────────────────────────

def _upload_icon(bucket: str, prefix: str, image_data: str) -> dict:
    """
    Sube un icono (base64 data URL) al bucket indicado.

    Returns: { url: str }
    Raises: HTTPException 400 — formato inválido · HTTPException 500
    """
    import base64
    import re
    import uuid

    match = re.match(r"^data:(.+);base64,(.+)$", image_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de imagen inválido. Se espera data:<mime>;base64,<datos>")

    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    ext = mime_type.split("/")[-1].replace("jpeg", "jpg")
    path = f"{prefix}-{uuid.uuid4().hex[:8]}.{ext}"

    supabase = get_supabase()
    try:
        supabase.storage.from_(bucket).upload(
            path, raw_bytes,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels._upload_icon] upload FAILED bucket=%s [%s] %s", bucket, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    url = supabase.storage.from_(bucket).get_public_url(path)
    logger.info("[levels._upload_icon] OK bucket=%s path=%s", bucket, path)
    return {"url": url}


def admin_upload_achievement_icon(image_data: str) -> dict:
    """
    Returns: { url } — URL pública del icono subido al bucket 'achievement-icons'.
    Raises: HTTPException 400 · HTTPException 500
    """
    logger.info("[levels.admin_upload_achievement_icon]")
    return _upload_icon("achievement-icons", "achievement", image_data)


def admin_upload_tier_icon(image_data: str) -> dict:
    """
    Returns: { url } — URL pública del icono subido al bucket 'level-tier-icons'.
    Raises: HTTPException 400 · HTTPException 500
    """
    logger.info("[levels.admin_upload_tier_icon]")
    return _upload_icon("level-tier-icons", "tier", image_data)


def admin_get_users_levels() -> list:
    """
    Returns: Todos los usuarios con su nivel/XP, ordenados por xp_total desc.
    Raises: HTTPException 500
    """
    logger.info("[levels.admin_get_users_levels]")
    supabase = get_supabase()
    try:
        resp = (
            supabase.table("user_levels")
            .select("user_id, level, xp_total, xp_current, xp_next, updated_at, profiles(name, email, avatar)")
            .order("xp_total", desc=True)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.admin_get_users_levels] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return resp.data or []


def admin_award(user_id: str, xp_amount: int, reason: str) -> dict:
    """
    Admin otorga XP manualmente a un usuario vía admin_award.

    Returns: { xp_awarded, new_level, leveled_up, skipped }
    Raises: HTTPException 404 — usuario no existe · HTTPException 500
    """
    logger.info("[levels.admin_award] user_id=%s xp=%s reason=%s", user_id, xp_amount, reason)
    supabase = get_supabase()

    try:
        profile = supabase.table("profiles").select("id").eq("id", user_id).limit(1).execute()
        if not profile.data:
            raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=supabase_error(exc))

    # Obtener id del achievement_type admin_award (no falla si no existe)
    admin_type_id = None
    try:
        atype = (
            supabase.table("achievement_types")
            .select("id")
            .eq("code", "admin_award")
            .limit(1)
            .execute()
        )
        if atype.data:
            admin_type_id = atype.data[0]["id"]
    except Exception:
        pass

    if admin_type_id:
        try:
            supabase.table("user_achievements").insert({
                "user_id": user_id,
                "achievement_type_id": admin_type_id,
                "obtained_at": datetime.now(timezone.utc).isoformat(),
                "metadata": {"reason": reason, "xp_amount": xp_amount},
            }).execute()
        except Exception as exc:
            logger.warning("[levels.admin_award] user_achievement insert failed (non-fatal) [%s]", supabase_error(exc))

    try:
        rpc_resp = supabase.rpc("award_xp", {
            "p_user_id": user_id,
            "p_amount": xp_amount,
            "p_reason": reason,
            "p_achievement_type_id": admin_type_id,
        }).execute()
        rpc_data = rpc_resp.data or {}
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.admin_award] award_xp RPC FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    return {
        "xp_awarded": xp_amount,
        "new_level": rpc_data.get("new_level", 0),
        "leveled_up": bool(rpc_data.get("leveled_up", False)),
        "skipped": False,
    }


def admin_get_all_achievements() -> list:
    """Returns todos los logros incluyendo inactivos."""
    logger.info("[levels.admin_get_all_achievements]")
    supabase = get_supabase()
    try:
        resp = supabase.table("achievement_types").select("*").order("xp_reward", desc=True).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.admin_get_all_achievements] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return resp.data or []


def admin_create_achievement(data: dict) -> dict:
    """
    Returns: Achievement creado.
    Raises: HTTPException 409 — code duplicado · HTTPException 500
    """
    logger.info("[levels.admin_create_achievement] code=%s", data.get("code"))
    supabase = get_supabase()

    try:
        existing = (
            supabase.table("achievement_types")
            .select("id")
            .eq("code", data["code"])
            .limit(1)
            .execute()
        )
        if existing.data:
            raise HTTPException(status_code=409, detail=f"Ya existe un logro con code='{data['code']}'.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=supabase_error(exc))

    try:
        resp = supabase.table("achievement_types").insert(data).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.admin_create_achievement] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not resp.data:
        raise HTTPException(status_code=500, detail="No se pudo crear el logro.")
    return resp.data[0]


def admin_update_achievement(achievement_id: str, update_data: dict) -> dict:
    """
    Returns: Achievement actualizado.
    Raises: HTTPException 400 — sin campos · HTTPException 404 · HTTPException 500
    """
    logger.info("[levels.admin_update_achievement] id=%s", achievement_id)
    supabase = get_supabase()

    if not update_data:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar.")

    try:
        resp = (
            supabase.table("achievement_types")
            .update(update_data)
            .eq("id", achievement_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.admin_update_achievement] FAILED id=%s [%s] %s", achievement_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not resp.data:
        raise HTTPException(status_code=404, detail="Logro no encontrado.")
    return resp.data[0]


def admin_get_tiers() -> list:
    """Returns todos los rangos de nivel."""
    logger.info("[levels.admin_get_tiers]")
    supabase = get_supabase()
    try:
        resp = supabase.table("level_tiers").select("*").order("min_level").execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.admin_get_tiers] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)
    return resp.data or []


def admin_create_tier(data: dict) -> dict:
    """
    Returns: Rango creado.
    Raises: HTTPException 400 — min > max · HTTPException 500
    """
    logger.info("[levels.admin_create_tier] name=%s min=%s max=%s", data.get("name"), data.get("min_level"), data.get("max_level"))
    supabase = get_supabase()

    if data.get("min_level", 0) > data.get("max_level", 0):
        raise HTTPException(status_code=400, detail="min_level no puede ser mayor que max_level.")

    try:
        resp = supabase.table("level_tiers").insert(data).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.admin_create_tier] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not resp.data:
        raise HTTPException(status_code=500, detail="No se pudo crear el rango.")
    return resp.data[0]


def admin_update_tier(tier_id: str, update_data: dict) -> dict:
    """
    Returns: Rango actualizado.
    Raises: HTTPException 400 · HTTPException 404 · HTTPException 500
    """
    logger.info("[levels.admin_update_tier] id=%s", tier_id)
    supabase = get_supabase()

    if not update_data:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar.")

    min_l = update_data.get("min_level")
    max_l = update_data.get("max_level")
    if min_l is not None and max_l is not None and min_l > max_l:
        raise HTTPException(status_code=400, detail="min_level no puede ser mayor que max_level.")

    try:
        resp = (
            supabase.table("level_tiers")
            .update(update_data)
            .eq("id", tier_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[levels.admin_update_tier] FAILED id=%s [%s] %s", tier_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not resp.data:
        raise HTTPException(status_code=404, detail="Rango no encontrado.")
    return resp.data[0]

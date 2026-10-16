import logging
from datetime import datetime, timezone

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import create_anon_client, get_supabase

logger = logging.getLogger(__name__)


def _normalize_rpc(data) -> dict:
    """Supabase RPCs can return a list or a dict depending on how they're defined."""
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


# Hitos que se otorgan al hacer check-in. register_daily_login() lleva la racha
# pero no otorga estos logros (verificado 2026-10-11: un miembro llegó a 7 días
# seguidos y nadie tenía streak_3/streak_7 ni XP por racha), así que los da
# este servicio. Son no repetibles: process_achievement los ignora si ya están.
_STREAK_MILESTONES = [(3, "streak_3"), (7, "streak_7"), (14, "streak_14"), (30, "streak_30"), (60, "streak_60"), (100, "streak_100")]
# Días desde el primer pago aprobado.
_MEMBERSHIP_MILESTONES = [(30, "member_1_month"), (182, "member_6_months"), (365, "member_1_year")]


def _earned_codes(supabase, user_id: str) -> set[str]:
    resp = (
        supabase.table("user_achievements")
        .select("achievement_types(code)")
        .eq("user_id", user_id)
        .execute()
    )
    return {(r.get("achievement_types") or {}).get("code") for r in (resp.data or [])}


def _membership_days(supabase, user_id: str) -> int:
    resp = (
        supabase.table("payments")
        .select("paid_at")
        .eq("user_id", user_id)
        .eq("status", "success")
        .not_.is_("paid_at", "null")
        .order("paid_at")
        .limit(1)
        .execute()
    )
    if not resp.data:
        return 0
    first_paid = datetime.fromisoformat(resp.data[0]["paid_at"])
    return (datetime.now(timezone.utc) - first_paid).days


def _award_milestones(user_id: str, best_streak: int) -> list[str]:
    """
    Otorga los hitos de racha y antigüedad que el usuario ya alcanzó y no tiene.
    `best_streak` es la racha más larga (no la actual), y se compara con >=:
    así quien llegó a un hito antes de que existiera esta entrega, o perdió la
    racha después, igual lo recibe. No fatal: un fallo sólo se registra.

    Returns:
        Códigos otorgados en esta llamada.
    """
    from app.services.levels import process_achievement

    supabase = get_supabase()
    try:
        earned = _earned_codes(supabase, user_id)
        due = [code for days, code in _STREAK_MILESTONES if best_streak >= days and code not in earned]
        pending_membership = [(d, c) for d, c in _MEMBERSHIP_MILESTONES if c not in earned]
        if pending_membership:
            member_days = _membership_days(supabase, user_id)
            due += [code for days, code in pending_membership if member_days >= days]
    except Exception as exc:
        logger.warning("[streaks._award_milestones] lookup FAILED user_id=%s [%s]", user_id, supabase_error(exc))
        return []

    awarded = []
    for code in due:
        try:
            result = process_achievement(user_id, code)
            if not result.get("skipped"):
                awarded.append(code)
        except Exception as exc:
            # 404 si el logro está inactivo: se respeta y se sigue con los demás.
            logger.warning("[streaks._award_milestones] %s FAILED user_id=%s [%s]", code, user_id, exc)
    if awarded:
        logger.info("[streaks._award_milestones] user_id=%s awarded=%s", user_id, awarded)
    return awarded


def checkin(token: str, user_id: str) -> dict:
    """
    Llama al RPC register_daily_login(), que usa auth.uid() internamente.
    Por eso el request debe ir autenticado con el JWT del usuario (no con el
    service-role client) para que auth.uid() se resuelva correctamente.
    Después otorga los hitos de racha/antigüedad alcanzados (_award_milestones).

    Returns:
        { status, current_streak, longest_streak, xp_awarded?, milestone_reached?, achievement?,
          new_achievements: [code] }
    Raises:
        HTTPException 500 — fallo del RPC
    """
    logger.info("[streaks.checkin] calling register_daily_login")
    client = create_anon_client()
    client.postgrest.auth(token)

    try:
        result = client.rpc("register_daily_login", {}).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[streaks.checkin] RPC FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    normalized = _normalize_rpc(result.data)
    logger.info(
        "[streaks.checkin] OK status=%s current_streak=%s",
        normalized.get("status"), normalized.get("current_streak"),
    )
    best_streak = max(int(normalized.get("current_streak") or 0), int(normalized.get("longest_streak") or 0))
    normalized["new_achievements"] = _award_milestones(user_id, best_streak)
    return normalized


def get_my_streak(user_id: str) -> dict:
    """
    Lee el estado de racha actual del usuario desde user_streaks.

    Returns:
        { current_streak, longest_streak, last_activity_date } — en 0/None si el usuario
        todavía no tiene fila en user_streaks (nunca hizo check-in).
    Raises:
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[streaks.get_my_streak] user_id=%s", user_id)
    supabase = get_supabase()

    try:
        result = (
            supabase.table("user_streaks")
            .select("current_streak, longest_streak, last_activity_date")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[streaks.get_my_streak] FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    row = result.data if result is not None else None
    if not row:
        return {"current_streak": 0, "longest_streak": 0, "last_activity_date": None}

    logger.info("[streaks.get_my_streak] OK current_streak=%s longest_streak=%s", row.get("current_streak"), row.get("longest_streak"))
    return row

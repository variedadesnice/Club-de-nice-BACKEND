import logging

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)


def get_public_profile(user_id: str) -> dict:
    """
    Devuelve el perfil público de un usuario: nombre, avatar, bio, ciudad,
    nivel, tier, logros, racha, cursos completados e impacto social.
    No expone datos privados (email, teléfono, fecha de nacimiento).

    Returns:
        {
            id, name, avatar, bio, city, role,
            level: { level, xp_current, xp_next, tier? },
            achievements: [...],
            streak: { current_streak, longest_streak },
            completed_courses: int,
            social_impact: int,
        }
    Raises:
        HTTPException 404 — perfil no encontrado
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[users.get_public_profile] user_id=%s", user_id)
    supabase = get_supabase()

    # 1. Perfil básico (sin datos privados)
    try:
        profile_resp = (
            supabase.table("profiles")
            .select("id, name, avatar, bio, city, role")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        profile = profile_resp.data
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[users.get_public_profile] profile FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no encontrado.")

    # 2. Nivel y tier
    level_data = None
    try:
        level_resp = (
            supabase.table("user_levels")
            .select("level, xp_current, xp_next")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if level_resp.data:
            row = level_resp.data
            # Intentamos obtener el tier para el nivel actual
            tier = None
            try:
                tier_resp = (
                    supabase.table("level_tiers")
                    .select("id, name, description, icon_url")
                    .lte("min_level", row["level"])
                    .gte("max_level", row["level"])
                    .limit(1)
                    .execute()
                )
                tier = tier_resp.data[0] if tier_resp.data else None
            except Exception:
                pass
            level_data = {
                "level": row["level"],
                "xp_current": row["xp_current"],
                "xp_next": row["xp_next"],
                "tier": tier,
            }
    except Exception as exc:
        logger.warning("[users.get_public_profile] level fetch failed user_id=%s [%s]", user_id, supabase_error(exc))

    # 3. Logros
    achievements = []
    try:
        ach_resp = (
            supabase.table("user_achievements")
            .select("id, earned_at:obtained_at, achievement:achievement_types(code, name, description, icon_url, xp_reward)")
            .eq("user_id", user_id)
            .order("obtained_at", desc=True)
            .execute()
        )
        achievements = ach_resp.data or []
    except Exception as exc:
        logger.warning("[users.get_public_profile] achievements fetch failed user_id=%s [%s]", user_id, supabase_error(exc))

    # 4. Racha
    streak = {"current_streak": 0, "longest_streak": 0}
    try:
        streak_resp = (
            supabase.table("user_streaks")
            .select("current_streak, longest_streak")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if streak_resp.data:
            streak = {
                "current_streak": streak_resp.data.get("current_streak", 0),
                "longest_streak": streak_resp.data.get("longest_streak", 0),
            }
    except Exception as exc:
        logger.warning("[users.get_public_profile] streak fetch failed user_id=%s [%s]", user_id, supabase_error(exc))

    # 5. Cursos completados
    completed_courses = 0
    try:
        courses_resp = (
            supabase.table("user_course_progress")
            .select("chapter_id", count="exact")
            .eq("user_id", user_id)
            .eq("completed", True)
            .execute()
        )
        completed_courses = courses_resp.count or 0
    except Exception as exc:
        logger.warning("[users.get_public_profile] courses fetch failed user_id=%s [%s]", user_id, supabase_error(exc))

    # 6. Impacto social (likes + comentarios en posts del usuario)
    social_impact = 0
    try:
        posts_resp = (
            supabase.table("posts")
            .select("id, likes, comments")
            .eq("user_id", user_id)
            .execute()
        )
        if posts_resp.data:
            for post in posts_resp.data:
                social_impact += (post.get("likes") or 0) + (post.get("comments") or 0)
    except Exception as exc:
        logger.warning("[users.get_public_profile] social_impact fetch failed user_id=%s [%s]", user_id, supabase_error(exc))

    logger.info("[users.get_public_profile] OK user_id=%s", user_id)
    return {
        "id": profile["id"],
        "name": profile.get("name"),
        "avatar": profile.get("avatar"),
        "bio": profile.get("bio"),
        "city": profile.get("city"),
        "role": profile.get("role"),
        "level": level_data,
        "achievements": achievements,
        "streak": streak,
        "completed_courses": completed_courses,
        "social_impact": social_impact,
    }

def get_admin_profile(user_id: str) -> dict:
    """
    Perfil completo para administradores: todo lo del perfil público más los
    datos de contacto y el estado de la suscripción.

    Existe aparte de `get_public_profile()` a propósito. Ese endpoint lo puede
    llamar cualquier miembro autenticado, así que agregarle el email y el
    teléfono los expondría a toda la comunidad; acá el `Depends(get_current_admin)`
    de la ruta es lo que separa una cosa de la otra. Si algún día hay que
    cambiar qué es privado, se cambia en un solo lugar: el que devuelve esto.

    Returns:
        Lo mismo que get_public_profile() más:
        {
            email, phone, gender, birthdate, subscription_status,
            subscription_expires_at, joined_at, last_payment: {...} | None
        }
    Raises:
        HTTPException 404 — perfil no encontrado
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[users.get_admin_profile] user_id=%s", user_id)
    perfil = get_public_profile(user_id)
    supabase = get_supabase()

    privados = {}
    try:
        resp = (
            supabase.table("profiles")
            .select("phone, gender, birthdate, subscription_status")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        privados = (resp.data if resp else None) or {}
    except Exception as exc:
        logger.warning("[users.get_admin_profile] private fields failed user_id=%s [%s]", user_id, supabase_error(exc))

    # El email vive en auth.users, no en profiles.
    email = None
    joined_at = None
    try:
        auth_resp = supabase.auth.admin.get_user_by_id(user_id)
        if auth_resp and auth_resp.user:
            email = auth_resp.user.email
            joined_at = getattr(auth_resp.user, "created_at", None)
            if joined_at is not None and not isinstance(joined_at, str):
                joined_at = joined_at.isoformat()
    except Exception as exc:
        logger.warning("[users.get_admin_profile] auth lookup failed user_id=%s [%s] %s", user_id, type(exc).__name__, exc)

    # Último pago: es lo que suele hacer falta para resolver un reclamo o
    # confirmar que quien escribe es quien pagó.
    ultimo_pago = None
    expires_at = None
    try:
        pagos = (
            supabase.table("payments")
            .select("id, status, plan, amount, amount_local, reference_number, payment_method, created_at, paid_at, expires_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if pagos and pagos.data:
            ultimo_pago = pagos.data[0]
    except Exception as exc:
        logger.warning("[users.get_admin_profile] last payment failed user_id=%s [%s]", user_id, supabase_error(exc))

    try:
        vigentes = (
            supabase.table("payments")
            .select("expires_at")
            .eq("user_id", user_id)
            .eq("status", "success")
            .order("expires_at", desc=True)
            .limit(1)
            .execute()
        )
        if vigentes and vigentes.data:
            expires_at = vigentes.data[0].get("expires_at")
    except Exception as exc:
        logger.warning("[users.get_admin_profile] expires_at failed user_id=%s [%s]", user_id, supabase_error(exc))

    perfil.update({
        "email": email,
        "phone": privados.get("phone"),
        "gender": privados.get("gender"),
        "birthdate": privados.get("birthdate"),
        "subscription_status": privados.get("subscription_status"),
        "subscription_expires_at": expires_at,
        "joined_at": joined_at,
        "last_payment": ultimo_pago,
    })
    logger.info("[users.get_admin_profile] OK user_id=%s", user_id)
    return perfil

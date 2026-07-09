import logging

from app.core.exceptions import supabase_error
from app.services import classroom as classroom_service
from app.services import levels as levels_service
from app.services import posts as posts_service
from app.services import streaks as streaks_service

logger = logging.getLogger(__name__)


def get_my_summary(user_id: str, token: str) -> dict:
    """
    Agrega en una sola respuesta los datos que Profile.tsx necesita: nivel,
    insignias, racha (registra el check-in diario, igual que /streaks/checkin),
    cursos completados e impacto social.

    Cada sección reutiliza la función de servicio existente tal cual (misma
    fuente de verdad que sus endpoints individuales) y está aislada en su
    propio try/except — un fallo en una sección no tumba las demás, igual que
    users.get_public_profile().
    """
    logger.info("[profile.get_my_summary] user_id=%s", user_id)

    level = None
    try:
        level = levels_service.get_user_level(user_id)
    except Exception as exc:
        logger.warning("[profile.get_my_summary] level FAILED user_id=%s [%s]", user_id, supabase_error(exc))

    achievements = []
    try:
        achievements = levels_service.get_my_achievements(user_id)
    except Exception as exc:
        logger.warning("[profile.get_my_summary] achievements FAILED user_id=%s [%s]", user_id, supabase_error(exc))

    streak = None
    try:
        streak = streaks_service.checkin(token)
    except Exception as exc:
        logger.warning("[profile.get_my_summary] streak FAILED user_id=%s [%s]", user_id, supabase_error(exc))

    completed_courses = 0
    try:
        completed_courses = classroom_service.get_completed_courses_count(user_id)["completedCourses"]
    except Exception as exc:
        logger.warning("[profile.get_my_summary] courses FAILED user_id=%s [%s]", user_id, supabase_error(exc))

    social_impact = 0
    try:
        social_impact = posts_service.get_social_impact(user_id)["totalImpact"]
    except Exception as exc:
        logger.warning("[profile.get_my_summary] social_impact FAILED user_id=%s [%s]", user_id, supabase_error(exc))

    logger.info("[profile.get_my_summary] OK user_id=%s", user_id)
    return {
        "level": level,
        "achievements": achievements,
        "streak": streak,
        "completedCourses": completed_courses,
        "socialImpact": social_impact,
    }

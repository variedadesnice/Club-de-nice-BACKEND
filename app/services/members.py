import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000  # PostgREST corta en 1000 filas por request

# Días antes del vencimiento a partir de los cuales se marca "por vencer".
_EXPIRING_SOON_DAYS = 7

_PROFILE_COLUMNS = (
    "id, name, avatar, role, subscription_status, gender, city, phone, birthdate, created_at"
)


def _get_email_map(supabase) -> dict:
    """Un solo llamado a la Auth admin API para resolver user_id -> email (evita N llamadas)."""
    try:
        users_resp = supabase.auth.admin.list_users()
        return {u.id: u.email for u in (users_resp or [])}
    except Exception as exc:
        logger.warning("[members._get_email_map] FAILED [%s] %s", type(exc).__name__, supabase_error(exc))
        return {}


def _fetch_all_profiles(supabase) -> list:
    rows: list = []
    offset = 0

    while True:
        try:
            result = (
                supabase.table("profiles")
                .select(_PROFILE_COLUMNS)
                .range(offset, offset + _PAGE_SIZE - 1)
                .execute()
            )
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[members._fetch_all_profiles] FAILED offset=%d [%s] %s", offset, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al leer perfiles: {msg}")

        page = result.data or []
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        offset += _PAGE_SIZE


def _fetch_expiry_map(supabase) -> dict:
    """
    user_id -> expires_at más lejano entre sus pagos aprobados. Una sola query en
    lugar de una por usuario (como hace auth._get_subscription_expires_at).
    """
    expiry: dict = {}
    offset = 0

    while True:
        try:
            result = (
                supabase.table("payments")
                .select("user_id, expires_at, plan")
                .eq("status", "success")
                .range(offset, offset + _PAGE_SIZE - 1)
                .execute()
            )
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[members._fetch_expiry_map] FAILED offset=%d [%s] %s", offset, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al leer pagos: {msg}")

        page = result.data or []
        for row in page:
            user_id = row.get("user_id")
            expires_at = row.get("expires_at")
            if not user_id or not expires_at:
                continue
            current = expiry.get(user_id)
            if current is None or expires_at > current["expires_at"]:
                expiry[user_id] = {"expires_at": expires_at, "plan": row.get("plan")}

        if len(page) < _PAGE_SIZE:
            return expiry
        offset += _PAGE_SIZE


def _days_until(expires_at: Optional[str]) -> Optional[int]:
    """Días que faltan para el vencimiento; negativo si ya pasó. None si no hay fecha válida."""
    if not expires_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return (parsed.date() - datetime.now(timezone.utc).date()).days


def _access_state(profile: dict, days_remaining: Optional[int]) -> str:
    """
    Estado de acceso efectivo, que no siempre coincide con subscription_status:
    el trigger sync_subscription_status solo corre cuando se toca la tabla
    payments, así que una suscripción puede haber vencido por el paso del tiempo
    sin que la fila del perfil se haya actualizado todavía. Por eso la fecha
    manda sobre el estado guardado.

    Valores: exempt | active | expiring_soon | expired | inactive
    """
    role = (profile.get("role") or "").strip().lower()
    if role in ("admin", "invitado"):
        return "exempt"

    status = (profile.get("subscription_status") or "").strip().lower()

    if days_remaining is not None and days_remaining < 0:
        return "expired"
    if status == "expired":
        return "expired"
    if status == "active":
        if days_remaining is not None and days_remaining <= _EXPIRING_SOON_DAYS:
            return "expiring_soon"
        return "active"
    return "inactive"


def _age_from_birthdate(value) -> Optional[int]:
    if not value:
        return None
    try:
        born = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None

    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return age if 0 <= age < 130 else None


def list_members() -> dict:
    """
    Admin — roster completo de miembros con su estado de acceso calculado.

    Returns:
        {
          "members": [{id, name, email, avatar, role, city, phone, age,
                       subscription_status, subscription_expires_at, plan,
                       access_state, days_remaining, created_at}],
          "summary": {total, active, expiring_soon, expired, inactive, exempt},
        }
    Raises:
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[members.list] fetching")
    supabase = get_supabase()

    profiles = _fetch_all_profiles(supabase)
    expiry_map = _fetch_expiry_map(supabase)
    email_map = _get_email_map(supabase)

    members = []
    summary = {"total": 0, "active": 0, "expiring_soon": 0, "expired": 0, "inactive": 0, "exempt": 0}

    for profile in profiles:
        user_id = profile.get("id")
        payment = expiry_map.get(user_id) or {}
        expires_at = payment.get("expires_at")
        days_remaining = _days_until(expires_at)
        state = _access_state(profile, days_remaining)

        members.append({
            "id": user_id,
            "name": profile.get("name"),
            "email": email_map.get(user_id),
            "avatar": profile.get("avatar"),
            "role": profile.get("role"),
            "city": profile.get("city"),
            "phone": profile.get("phone"),
            "age": _age_from_birthdate(profile.get("birthdate")),
            "subscription_status": profile.get("subscription_status"),
            "subscription_expires_at": expires_at,
            "plan": payment.get("plan"),
            "access_state": state,
            "days_remaining": days_remaining,
            "created_at": profile.get("created_at"),
        })

        summary["total"] += 1
        summary[state] = summary.get(state, 0) + 1

    # Los que requieren atención primero: vencidos, luego por vencer (el más
    # urgente arriba), después el resto por nombre.
    state_priority = {"expired": 0, "expiring_soon": 1, "inactive": 2, "active": 3, "exempt": 4}
    members.sort(key=lambda m: (
        state_priority.get(m["access_state"], 9),
        m["days_remaining"] if m["days_remaining"] is not None else 99999,
        (m["name"] or "").lower(),
    ))

    logger.info("[members.list] returned %d items", len(members))
    return {"members": members, "summary": summary}

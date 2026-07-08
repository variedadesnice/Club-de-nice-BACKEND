import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr

from app.core.deps import get_current_admin, require_service_role
from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase
from app.core.config import get_settings
from app.services import email as email_service

# Rutas públicas — montadas bajo /api/auth
public_router = APIRouter()

# Rutas admin — montadas bajo /api/admin/emails
admin_router = APIRouter()

logger = logging.getLogger(__name__)


# ─── Password reset (público) ────────────────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


@public_router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest):
    """
    Genera un enlace de recuperación via Supabase admin y lo envía por Resend.
    Siempre devuelve 200 para no revelar si el email existe o no.
    """
    s = get_settings()
    supabase = get_supabase()
    email = str(body.email).lower()

    # Buscar nombre del usuario
    name = "miembro"
    try:
        auth_user = None
        users_resp = supabase.auth.admin.list_users()
        for u in (users_resp or []):
            if u.email and u.email.lower() == email:
                auth_user = u
                break
        if auth_user:
            profile_resp = (
                supabase.table("profiles")
                .select("name")
                .eq("id", auth_user.id)
                .maybe_single()
                .execute()
            )
            if profile_resp.data:
                name = profile_resp.data.get("name") or name
    except Exception as exc:
        logger.warning("[emails.forgot_password] profile lookup failed: %s", supabase_error(exc))

    # Generar enlace de recuperación con Supabase admin
    try:
        link_resp = supabase.auth.admin.generate_link({
            "type": "recovery",
            "email": email,
            "options": {"redirect_to": f"{s.app_url}/reset-password"},
        })
        reset_link = link_resp.properties.action_link
    except Exception as exc:
        logger.warning("[emails.forgot_password] generate_link failed for %s: %s", email, supabase_error(exc))
        return {"message": "Si el email está registrado, recibirás un correo en breve."}

    email_service.send_password_reset(email, name, reset_link)
    logger.info("[emails.forgot_password] reset link sent to %s", email)
    return {"message": "Si el email está registrado, recibirás un correo en breve."}


# ─── Recordatorios de renovación ─────────────────────────────────────────────

def _run_reminders() -> dict:
    result = email_service.dispatch_renewal_reminders()
    return {
        "sent_5_days": result["5_days"],
        "sent_1_day": result["1_day"],
        "sent_expired": result["expired"],
        "errors": result["errors"],
    }


@admin_router.post("/renewal-reminders")
def send_renewal_reminders(_: dict = Depends(get_current_admin)):
    """Admin manual — dispara los correos de renovación del día."""
    return _run_reminders()


@admin_router.post("/renewal-reminders/cron")
def renewal_reminders_cron(_: None = Depends(require_service_role)):
    """
    Endpoint para pg_cron — autenticado con el service_role_key de Supabase
    (nunca expira). No usa JWT de usuario.
    """
    logger.info("[emails.cron] renewal reminders triggered by cron")
    return _run_reminders()

"""
Email service — todas las comunicaciones transaccionales a través de Resend.

Cada función send_* es fire-and-forget: si el envío falla, loguea el error
pero nunca propaga la excepción para no bloquear la operación principal.
"""
import logging
import threading
from datetime import date, timedelta
from typing import Optional

import resend

from app.core.config import get_settings
from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)


# ─── Helpers de plantilla ────────────────────────────────────────────────────

def _base(preheader: str, body: str) -> str:
    s = get_settings()
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,sans-serif;">
<span style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{preheader}</span>
<table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#f1f5f9;">
  <tr><td align="center" style="padding:40px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="max-width:560px;">

      <!-- Logo / Header -->
      <tr><td style="background:#db2777;border-radius:20px 20px 0 0;padding:32px 40px;text-align:center;">
        <p style="margin:0;color:#fff;font-size:22px;font-weight:900;letter-spacing:-0.3px;">{s.app_name}</p>
      </td></tr>

      <!-- Body -->
      <tr><td style="background:#ffffff;border-radius:0 0 20px 20px;padding:40px;border:1px solid #e2e8f0;border-top:0;">
        {body}
        <hr style="border:0;border-top:1px solid #f1f5f9;margin:32px 0 0;">
        <p style="margin:16px 0 0;color:#94a3b8;font-size:12px;line-height:1.5;">
          Este es un correo automático, por favor no respondas a este mensaje.<br>
          © {s.app_name} · Todos los derechos reservados.
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def _btn(text: str, url: str, color: str = "#db2777") -> str:
    return (
        f'<table cellpadding="0" cellspacing="0" role="presentation" style="margin:28px 0;">'
        f'<tr><td style="border-radius:12px;background:{color};">'
        f'<a href="{url}" style="display:inline-block;padding:14px 32px;color:#fff;'
        f'text-decoration:none;font-weight:900;font-size:15px;border-radius:12px;">{text}</a>'
        f'</td></tr></table>'
    )


def _h1(text: str) -> str:
    return f'<h1 style="margin:0 0 12px;color:#0f172a;font-size:24px;font-weight:900;line-height:1.2;">{text}</h1>'


def _p(text: str) -> str:
    return f'<p style="margin:0 0 16px;color:#475569;font-size:15px;line-height:1.6;">{text}</p>'


def _badge(text: str, color: str = "#db2777") -> str:
    return (
        f'<span style="display:inline-block;background:{color}15;color:{color};'
        f'border-radius:8px;padding:4px 12px;font-size:13px;font-weight:900;">{text}</span>'
    )


# ─── Envío central ────────────────────────────────────────────────────────────

def _send(to: str, subject: str, html: str) -> bool:
    """Envía un email. Retorna True si se envió, False si falló o no está configurado."""
    s = get_settings()
    if not s.is_email_configured():
        logger.warning("[email] RESEND_API_KEY no configurado — email omitido: %s", subject)
        return False
    try:
        resend.api_key = s.resend_api_key
        resend.Emails.send({
            "from": s.from_email,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        logger.info("[email] sent '%s' → %s", subject, to)
        return True
    except Exception as exc:
        logger.error("[email] FAILED '%s' → %s: %s", subject, to, exc)
        return False


def _send_async(to: str, subject: str, html: str) -> None:
    """Fire-and-forget: envía en un hilo daemon para no bloquear la request."""
    threading.Thread(target=_send, args=(to, subject, html), daemon=True).start()


# ─── Plantillas de email ─────────────────────────────────────────────────────

def _welcome_html(name: str) -> str:
    s = get_settings()
    body = (
        _h1(f"¡Bienvenido/a, {name}! 🎉")
        + _p("Tu acceso a <strong>El Club de Nice</strong> ya está activo. "
             "Explora el muro de la comunidad, accede a los cursos y no te pierdas las sesiones en vivo.")
        + _btn("Ir al Club", s.app_url)
        + _p('Si tienes alguna pregunta, no dudes en escribirnos.')
    )
    return _base(f"¡Bienvenido/a al Club, {name}!", body)


def _payment_approved_html(name: str, plan: str, expires_at: Optional[str]) -> str:
    s = get_settings()
    plan_labels = {"1m": "1 mes", "3m": "3 meses", "6m": "6 meses", "1y": "1 año", "indefinido": "Indefinido"}
    plan_label = plan_labels.get(plan, plan)
    expiry_line = (
        f"Tu suscripción vence el <strong>{expires_at[:10]}</strong>."
        if expires_at else
        "Tu suscripción es <strong>indefinida</strong> — sin fecha de vencimiento."
    )
    body = (
        _h1("¡Tu pago fue aprobado! ✅")
        + _p(f"Hola <strong>{name}</strong>, tu suscripción ha sido revisada y <strong>aprobada</strong>.")
        + f'<table cellpadding="0" cellspacing="0" style="background:#f8fafc;border-radius:12px;padding:20px;margin:0 0 20px;width:100%;box-sizing:border-box;">'
        + f'<tr><td><p style="margin:0 0 8px;color:#64748b;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;">Plan</p>'
        + f'<p style="margin:0;color:#0f172a;font-size:18px;font-weight:900;">{_badge(plan_label)}</p></td></tr>'
        + f'<tr><td style="padding-top:16px;"><p style="margin:0;color:#475569;font-size:14px;">{expiry_line}</p></td></tr>'
        + '</table>'
        + _btn("Acceder al Club", s.app_url)
    )
    return _base("¡Tu pago fue aprobado!", body)


def _password_reset_html(name: str, reset_link: str) -> str:
    body = (
        _h1("Recupera tu contraseña 🔑")
        + _p(f"Hola <strong>{name}</strong>, recibimos una solicitud para restablecer la contraseña de tu cuenta.")
        + _btn("Restablecer contraseña", reset_link, "#0f172a")
        + _p("Este enlace vence en <strong>1 hora</strong>. Si no solicitaste el cambio, puedes ignorar este correo.")
        + f'<p style="margin:0;color:#94a3b8;font-size:12px;word-break:break-all;">Si el botón no funciona, copia este enlace: {reset_link}</p>'
    )
    return _base("Restablece tu contraseña", body)


def _renewal_reminder_html(name: str, days_left: int, expires_date: str) -> str:
    s = get_settings()
    urgency_color = "#ef4444" if days_left <= 1 else "#f59e0b"
    urgency_text = "¡mañana!" if days_left == 1 else f"en {days_left} días"
    body = (
        _h1(f"Tu suscripción vence {urgency_text} ⏰")
        + _p(f"Hola <strong>{name}</strong>, te avisamos que tu acceso a El Club de Nice "
             f"vence el <strong>{expires_date}</strong>.")
        + _p("Para seguir disfrutando de todos los beneficios, renueva tu suscripción a tiempo.")
        + _btn("Renovar suscripción", f"{s.app_url}/renovar", urgency_color)
        + _p("Si ya realizaste el pago, el admin lo revisará pronto y tu acceso se extenderá automáticamente.")
    )
    return _base(f"Tu suscripción vence {urgency_text}", body)


def _raffle_winner_html(name: str, raffle_title: str) -> str:
    s = get_settings()
    body = (
        _h1("¡Felicidades, ganaste! 🎉")
        + _p(f"Hola <strong>{name}</strong>, tenemos una gran noticia: fuiste elegido/a ganador/a del sorteo "
             f"<strong>{raffle_title}</strong>.")
        + _p("Nuestro equipo se pondrá en contacto contigo pronto para coordinar la entrega de tu premio.")
        + _btn("Ir al Club", s.app_url, "#16a34a")
    )
    return _base(f"¡Ganaste el sorteo {raffle_title}!", body)


def _expired_html(name: str) -> str:
    s = get_settings()
    body = (
        _h1("Tu suscripción ha vencido 😔")
        + _p(f"Hola <strong>{name}</strong>, tu suscripción a El Club de Nice <strong>ha expirado</strong>. "
             "Tu acceso está temporalmente suspendido.")
        + _p("Renueva para volver a disfrutar de la comunidad, cursos y sesiones en vivo.")
        + _btn("Renovar ahora", f"{s.app_url}/renovar", "#db2777")
    )
    return _base("Tu suscripción ha expirado", body)


# ─── API pública de envío ────────────────────────────────────────────────────

def send_welcome(to: str, name: str) -> None:
    """Bienvenida tras registro estándar o invitación."""
    _send_async(to, f"¡Bienvenido/a a El Club de Nice, {name}!", _welcome_html(name))


def send_payment_approved(to: str, name: str, plan: str, expires_at: Optional[str]) -> None:
    """Confirmación de pago aprobado y suscripción activa."""
    _send_async(to, "¡Tu pago fue aprobado! Ya tienes acceso al Club", _payment_approved_html(name, plan, expires_at))


def send_password_reset(to: str, name: str, reset_link: str) -> None:
    """Enlace de restablecimiento de contraseña generado via Supabase admin."""
    _send(to, "Restablece tu contraseña — El Club de Nice", _password_reset_html(name, reset_link))


def send_renewal_reminder(to: str, name: str, days_left: int, expires_date: str) -> None:
    """Aviso de renovación (5 días o 1 día antes)."""
    subject = (
        "¡Tu suscripción vence mañana! — El Club de Nice"
        if days_left <= 1
        else f"Tu suscripción vence en {days_left} días — El Club de Nice"
    )
    _send(to, subject, _renewal_reminder_html(name, days_left, expires_date))


def send_expired_notice(to: str, name: str) -> None:
    """Aviso de cuenta vencida."""
    _send(to, "Tu suscripción ha vencido — El Club de Nice", _expired_html(name))


def send_raffle_winner(to: str, name: str, raffle_title: str) -> None:
    """Notificación a un ganador de sorteo tras el draw."""
    _send_async(to, f'¡Ganaste el sorteo "{raffle_title}"! 🎉', _raffle_winner_html(name, raffle_title))


# ─── Lógica de recordatorios automáticos ─────────────────────────────────────

def _fetch_expiring(days_ahead: int) -> list[dict]:
    """Devuelve pagos activos cuyo expires_at cae exactamente en today+days_ahead."""
    supabase = get_supabase()
    start = (date.today() + timedelta(days=days_ahead)).isoformat()
    end = (date.today() + timedelta(days=days_ahead + 1)).isoformat()
    try:
        resp = (
            supabase.table("payments")
            .select("user_id, plan, expires_at, profiles(name)")
            .eq("status", "success")
            .gte("expires_at", start)
            .lt("expires_at", end)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.error("[email.renewal] fetch expiring days=%d FAILED: %s", days_ahead, supabase_error(exc))
        return []


def _fetch_expired_today() -> list[dict]:
    """Devuelve pagos que expiraron en las últimas 24 horas."""
    supabase = get_supabase()
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=1)).isoformat()
    try:
        resp = (
            supabase.table("payments")
            .select("user_id, plan, expires_at, profiles(name)")
            .eq("status", "success")
            .gte("expires_at", start)
            .lt("expires_at", end)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.error("[email.renewal] fetch expired_today FAILED: %s", supabase_error(exc))
        return []


def _get_email(user_id: str) -> Optional[str]:
    try:
        resp = get_supabase().auth.admin.get_user_by_id(user_id)
        return resp.user.email if resp.user else None
    except Exception:
        return None


def dispatch_renewal_reminders() -> dict:
    """
    Envía correos de renovación. Llamar una vez al día vía cron.
    Retorna un resumen de cuántos correos se enviaron por categoría.
    """
    logger.info("[email.renewal] starting dispatch")
    sent = {"5_days": 0, "1_day": 0, "expired": 0, "errors": 0}

    for days, key in [(5, "5_days"), (1, "1_day")]:
        for row in _fetch_expiring(days):
            user_id = row.get("user_id")
            name = (row.get("profiles") or {}).get("name") or "miembro"
            expires_at = row.get("expires_at", "")
            expires_date = expires_at[:10] if expires_at else "—"
            to = _get_email(user_id)
            if not to:
                sent["errors"] += 1
                continue
            ok = send_renewal_reminder(to, name, days, expires_date)
            if ok:
                sent[key] += 1
            else:
                sent["errors"] += 1

    for row in _fetch_expired_today():
        user_id = row.get("user_id")
        name = (row.get("profiles") or {}).get("name") or "miembro"
        to = _get_email(user_id)
        if not to:
            sent["errors"] += 1
            continue
        ok = send_expired_notice(to, name)
        if ok:
            sent["expired"] += 1
        else:
            sent["errors"] += 1

    logger.info("[email.renewal] done — %s", sent)
    return sent

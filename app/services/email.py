"""
Email service — todas las comunicaciones transaccionales a través de Resend.

Cada función send_* devuelve True si el correo salió y False si falló o si
Resend no está configurado. Nunca propagan excepciones, para no tumbar la
operación principal por un fallo de correo.

Los envíos son síncronos a propósito: el backend corre en Vercel Functions,
que congela la ejecución en cuanto se devuelve la respuesta HTTP. Un envío
en un hilo de fondo se perdería sin dejar rastro.
"""
import logging
from datetime import date, datetime, timedelta, timezone
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


def _invitation_html(invite_link: str) -> str:
    body = (
        _h1("Has sido invitado/a 🎟️")
        + _p("Alguien del equipo de <strong>El Club de Nice</strong> te reservó un lugar en la comunidad. "
             "El acceso es solo por invitación, y esta es la tuya.")
        + _btn("Completar mi registro", invite_link)
        + _p("El enlace ya trae tu email vinculado — solo tienes que elegir tu nombre y contraseña.")
        + f'<p style="margin:0;color:#94a3b8;font-size:12px;word-break:break-all;">Si el botón no funciona, copia este enlace: {invite_link}</p>'
    )
    return _base("Tu invitación a El Club de Nice", body)


def _payment_rejected_html(name: str, account_deleted: bool) -> str:
    s = get_settings()
    if account_deleted:
        detail = _p(
            "Revisamos tu comprobante y no pudimos validarlo, así que tu registro no se completó. "
            "Puedes volver a registrarte con los datos corregidos — el email queda libre de nuevo."
        ) + _btn("Registrarme de nuevo", s.app_url, "#0f172a")
    else:
        detail = _p(
            "Revisamos tu comprobante y no pudimos validarlo, por lo que tu suscripción sigue sin renovarse. "
            "Verifica los datos de tu transferencia y envía un comprobante nuevo."
        ) + _btn("Enviar otro comprobante", f"{s.app_url}/renovar", "#0f172a")
    body = (
        _h1("No pudimos validar tu pago")
        + _p(f"Hola <strong>{name}</strong>:")
        + detail
        + _p("Si crees que se trata de un error, responde a este correo y lo revisamos contigo.")
    )
    return _base("No pudimos validar tu pago", body)


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

def send_welcome(to: str, name: str) -> bool:
    """Bienvenida tras registro estándar o invitación."""
    return _send(to, f"¡Bienvenido/a a El Club de Nice, {name}!", _welcome_html(name))


def send_invitation(to: str, invite_link: str) -> bool:
    """Enlace de registro enviado al crear una invitación desde el panel admin."""
    return _send(to, "Tu invitación a El Club de Nice 🎟️", _invitation_html(invite_link))


def send_payment_approved(to: str, name: str, plan: str, expires_at: Optional[str]) -> bool:
    """Confirmación de pago aprobado y suscripción activa."""
    return _send(to, "¡Tu pago fue aprobado! Ya tienes acceso al Club", _payment_approved_html(name, plan, expires_at))


def send_payment_rejected(to: str, name: str, account_deleted: bool) -> bool:
    """
    Aviso de comprobante rechazado.

    `account_deleted` distingue los dos caminos de payments.reject_payment: si
    era el pago de registro, la cuenta se borró y toca registrarse de nuevo; si
    era una renovación, la cuenta sigue viva y solo hace falta otro comprobante.
    """
    return _send(to, "No pudimos validar tu pago — El Club de Nice", _payment_rejected_html(name, account_deleted))


def send_password_reset(to: str, name: str, reset_link: str) -> bool:
    """Enlace de restablecimiento de contraseña generado via Supabase admin."""
    return _send(to, "Restablece tu contraseña — El Club de Nice", _password_reset_html(name, reset_link))


_EXPIRED_SUBJECT = "Tu suscripción ha vencido — El Club de Nice"


def _renewal_subject(days_left: int) -> str:
    return (
        "¡Tu suscripción vence mañana! — El Club de Nice"
        if days_left <= 1
        else f"Tu suscripción vence en {days_left} días — El Club de Nice"
    )


def send_renewal_reminder(to: str, name: str, days_left: int, expires_date: str) -> bool:
    """Aviso de renovación (5 días o 1 día antes)."""
    return _send(to, _renewal_subject(days_left), _renewal_reminder_html(name, days_left, expires_date))


def send_expired_notice(to: str, name: str) -> bool:
    """Aviso de cuenta vencida."""
    return _send(to, _EXPIRED_SUBJECT, _expired_html(name))


def send_raffle_winner(to: str, name: str, raffle_title: str) -> bool:
    """Notificación a un ganador de sorteo tras el draw."""
    return _send(to, f'¡Ganaste el sorteo "{raffle_title}"! 🎉', _raffle_winner_html(name, raffle_title))


# ─── Lógica de recordatorios automáticos ─────────────────────────────────────
#
# El cron llama esto por HTTP (pg_net) una vez al día, y pg_net corta la
# petición a los pocos segundos. Por eso todo va en pocas idas y vueltas fijas,
# sin importar cuántos usuarios toque: 3 consultas a la base, 1 a Auth para
# los emails y 1 envío por lotes a Resend por cada 100 correos.
#
# Cada usuario se evalúa por su vencimiento MÁS LEJANO, no pago por pago: quien
# renovó antes de tiempo tiene un pago viejo que igual vence, y no debe recibir
# "tu suscripción vence" ni "ha vencido" por ése.

_RESEND_BATCH_LIMIT = 100


def _parse_expiry_date(expires_at: str) -> Optional[date]:
    try:
        return datetime.fromisoformat(expires_at).astimezone(timezone.utc).date()
    except (TypeError, ValueError):
        return None


def _latest_expiry_by_user(since: date) -> dict[str, dict]:
    """
    user_id -> {"expires_on": date, "name": str} con el vencimiento más lejano
    de cada usuario, considerando sólo pagos aprobados que vencen desde `since`.
    Un pago anterior a `since` nunca puede ser el más lejano de un usuario que
    nos interese, así que no hace falta traerlo.
    """
    resp = (
        get_supabase().table("payments")
        .select("user_id, expires_at, profiles(name)")
        .eq("status", "success")
        .gte("expires_at", since.isoformat())
        .execute()
    )
    latest: dict[str, dict] = {}
    for row in resp.data or []:
        expires_on = _parse_expiry_date(row.get("expires_at"))
        if not expires_on:
            continue
        current = latest.get(row["user_id"])
        if current is None or expires_on > current["expires_on"]:
            latest[row["user_id"]] = {
                "expires_on": expires_on,
                "name": (row.get("profiles") or {}).get("name") or "miembro",
            }
    return latest


def _users_without_reminder() -> set[str]:
    """
    Usuarios a los que no se les avisa nada: los que tienen un plan indefinido
    aprobado (expires_at NULL, nunca vencen) y los que ya mandaron un pago que
    está esperando revisión (ya renovaron; falta que se apruebe).
    """
    supabase = get_supabase()
    indefinite = (
        supabase.table("payments").select("user_id")
        .eq("status", "success").is_("expires_at", "null").execute()
    )
    pending = supabase.table("payments").select("user_id").eq("status", "pending").execute()
    return {r["user_id"] for r in (indefinite.data or []) + (pending.data or [])}


def _get_email_map() -> dict[str, str]:
    """user_id -> email con la Auth admin API, paginando (list_users trae 50 por defecto)."""
    emails: dict[str, str] = {}
    page = 1
    while True:
        users = get_supabase().auth.admin.list_users(page=page, per_page=1000)
        for u in users or []:
            if u.email:
                emails[u.id] = u.email
        if not users or len(users) < 1000:
            return emails
        page += 1


def _send_batch(messages: list[dict]) -> int:
    """
    Envía en lotes de hasta 100 con la Batch API de Resend (un solo request por
    lote, así no se choca con el límite de ~2 requests/segundo). Devuelve
    cuántos salieron; un lote que falla se registra y se cuenta como no enviado.
    """
    s = get_settings()
    if not messages:
        return 0
    if not s.is_email_configured():
        logger.warning("[email] RESEND_API_KEY no configurado — %d emails omitidos", len(messages))
        return 0
    resend.api_key = s.resend_api_key
    sent = 0
    for i in range(0, len(messages), _RESEND_BATCH_LIMIT):
        chunk = [{"from": s.from_email, **m} for m in messages[i:i + _RESEND_BATCH_LIMIT]]
        try:
            resend.Batch.send(chunk)
            sent += len(chunk)
            for m in chunk:
                logger.info("[email] sent '%s' → %s", m["subject"], m["to"][0])
        except Exception as exc:
            logger.error("[email] batch FAILED (%d emails): %s", len(chunk), exc)
    return sent


def dispatch_renewal_reminders() -> dict:
    """
    Envía los correos de renovación del día. Llamar una vez al día vía cron.

    Con hoy = fecha UTC, a cada usuario según su vencimiento más lejano:
      - vence hoy+5  → aviso de 5 días
      - vence hoy+1  → aviso de 1 día
      - venció ayer  → aviso de suscripción vencida. Se manda al día siguiente
        y no el mismo día para no avisarle "ha vencido" a quien vence más tarde
        hoy y todavía tiene acceso.
    Cada caso corresponde a una sola fecha, así que correrlo dos veces el mismo
    día no cambia a quién le toca.

    Retorna un resumen de cuántos correos se enviaron por categoría.
    """
    today = datetime.now(timezone.utc).date()
    logger.info("[email.renewal] starting dispatch today=%s", today)
    sent = {"5_days": 0, "1_day": 0, "expired": 0, "errors": 0}

    try:
        latest = _latest_expiry_by_user(since=today - timedelta(days=1))
        skip = _users_without_reminder()
    except Exception as exc:
        logger.error("[email.renewal] fetch payments FAILED: %s", supabase_error(exc), exc_info=True)
        sent["errors"] += 1
        return sent

    targets: dict[str, list[tuple[str, dict]]] = {"5_days": [], "1_day": [], "expired": []}
    for user_id, info in latest.items():
        if user_id in skip:
            continue
        delta = (info["expires_on"] - today).days
        key = {5: "5_days", 1: "1_day", -1: "expired"}.get(delta)
        if key:
            targets[key].append((user_id, info))

    if not any(targets.values()):
        logger.info("[email.renewal] done (nadie para avisar hoy) — %s", sent)
        return sent

    try:
        email_map = _get_email_map()
    except Exception as exc:
        logger.error("[email.renewal] fetch emails FAILED: %s", supabase_error(exc), exc_info=True)
        sent["errors"] += sum(len(t) for t in targets.values())
        return sent

    for key, days in (("5_days", 5), ("1_day", 1), ("expired", None)):
        messages = []
        for user_id, info in targets[key]:
            to = email_map.get(user_id)
            if not to:
                logger.warning("[email.renewal] sin email user_id=%s", user_id)
                sent["errors"] += 1
                continue
            if days is None:
                subject, html = _EXPIRED_SUBJECT, _expired_html(info["name"])
            else:
                subject = _renewal_subject(days)
                html = _renewal_reminder_html(info["name"], days, info["expires_on"].isoformat())
            messages.append({"to": [to], "subject": subject, "html": html})
        delivered = _send_batch(messages)
        sent[key] += delivered
        sent["errors"] += len(messages) - delivered

    logger.info("[email.renewal] done — %s", sent)
    return sent

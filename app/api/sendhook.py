"""
Receptor de webhooks de SendHook.

URL pública a registrar del lado de SendHook:

    POST https://<tu-backend>/api/webhooks/sendhook

Esta ruta es pública a propósito (SendHook no tiene una sesión nuestra), así
que la autenticación es la firma HMAC del header `X-SendHook-Signature`.
Nunca se toca nada antes de validarla: sin eso, cualquiera que conozca la URL
podría mandarnos un `pago.conciliado` inventado y activarse una suscripción.

Tres reglas que impone SendHook y que se respetan acá:

1. Responder 2xx rápido (más de 5s cuenta como fallo y lo reintentan). La
   aprobación en sí es rápida; el correo de bienvenida ya sale fire-and-forget
   desde approve_payment.
2. Descartar repetidos por `X-SendHook-Event-Id`. Si nuestro 2xx se pierde,
   nos reenvían el mismo evento; sin idempotencia aprobaríamos dos veces.
3. Verificar la firma sobre el cuerpo CRUDO, byte por byte.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from app.core.supabase import get_supabase
from app.services import payments as payments_service
from app.services import sendhook

logger = logging.getLogger(__name__)

router = APIRouter()

_EVENTS_TABLE = "sendhook_webhook_events"


def _already_processed(supabase, event_id: str) -> bool:
    try:
        resp = supabase.table(_EVENTS_TABLE).select("event_id").eq("event_id", event_id).maybe_single().execute()
        return bool(resp and resp.data)
    except Exception as exc:
        # Si la tabla no existe todavía, no podemos garantizar idempotencia.
        # Se sigue igual (aprobar es idempotente por el chequeo de 'pending'
        # en approve_from_sendhook), pero se deja constancia en el log.
        logger.warning("[sendhook.webhook] No se pudo consultar %s: %s", _EVENTS_TABLE, exc)
        return False


def _mark_processed(supabase, event_id: str, evento: str, referencia_externa: str) -> None:
    try:
        supabase.table(_EVENTS_TABLE).insert({
            "event_id": event_id,
            "event_type": evento,
            "referencia_externa": referencia_externa,
        }).execute()
    except Exception as exc:
        logger.warning("[sendhook.webhook] No se pudo registrar el evento %s: %s", event_id, exc)


@router.post("/sendhook")
async def recibir_webhook_sendhook(
    request: Request,
    x_sendhook_event_id: str = Header(..., alias="X-SendHook-Event-Id"),
    x_sendhook_timestamp: str = Header(..., alias="X-SendHook-Timestamp"),
    x_sendhook_signature: str = Header(..., alias="X-SendHook-Signature"),
):
    if not sendhook.is_webhook_configured():
        # Mejor un 503 que un 401: le dice a SendHook que reintente en vez de
        # dar la entrega por fallida mientras nos falta configurar el secreto.
        logger.error("[sendhook.webhook] SENDHOOK_WEBHOOK_SECRET sin configurar, se rechaza el evento.")
        raise HTTPException(status_code=503, detail="Webhook no configurado.")

    raw_body = await request.body()

    if not sendhook.verificar_firma_webhook(raw_body, x_sendhook_timestamp, x_sendhook_signature):
        logger.warning("[sendhook.webhook] Firma inválida o fuera de rango (event_id=%s).", x_sendhook_event_id)
        raise HTTPException(status_code=401, detail="Firma inválida.")

    supabase = get_supabase()
    if _already_processed(supabase, x_sendhook_event_id):
        logger.info("[sendhook.webhook] event_id=%s ya procesado, se ignora.", x_sendhook_event_id)
        return {"ok": True}

    try:
        evento = await request.json()
    except Exception:
        logger.warning("[sendhook.webhook] Cuerpo no es JSON válido (event_id=%s).", x_sendhook_event_id)
        raise HTTPException(status_code=400, detail="Cuerpo inválido.")

    tipo = evento.get("evento")
    pedido = evento.get("pedido") or {}
    # `referencia_externa` es el id de NUESTRO pago: es lo que mandamos al
    # registrar el pedido en _verify_payment_automatically.
    payment_id = pedido.get("referencia_externa")

    logger.info("[sendhook.webhook] evento=%s payment_id=%s event_id=%s", tipo, payment_id, x_sendhook_event_id)

    if tipo == "pago.conciliado" and payment_id:
        payments_service.approve_from_sendhook(str(payment_id), evento.get("pago"), "webhook")
    else:
        logger.info("[sendhook.webhook] Evento sin acción asociada (evento=%s).", tipo)

    _mark_processed(supabase, x_sendhook_event_id, tipo or "", str(payment_id or ""))
    return {"ok": True}

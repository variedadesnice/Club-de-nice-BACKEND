"""
Cliente de la API de SendHook (pasarela de verificación de pagos móviles).

Encapsula todo lo que habla con SendHook para que `payments.py` no tenga que
saber nada de HTTP, firmas ni formatos de payload:

- `verificar_pago()`   -> POST /pagos/verificar   (consulta puntual)
- `registrar_pedido()` -> POST /pedidos           (pre-registro + webhook)
- `consultar_pedido()` -> GET  /pedidos/{ref}     (red de seguridad)
- `cancelar_pedido()`  -> POST /pedidos/{ref}/cancelar
- `verificar_firma_webhook()`                     (HMAC del webhook entrante)

El flujo recomendado por SendHook es pedidos + webhook: registramos el pago
que esperamos y ellos nos avisan cuando llega, en vez de preguntar en loop.
Igual mantenemos `/pagos/verificar` y `GET /pedidos/{ref}` como red de
seguridad por si el webhook no está configurado o no llega.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 20.0

# Tolerancia del anti-replay del webhook: un evento firmado hace más de esto
# se descarta aunque la firma sea válida (es el valor que sugiere SendHook).
WEBHOOK_TOLERANCE_SECONDS = 300

# Cuántos minutos hacia atrás busca SendHook un pago sin consumir.
VENTANA_MINUTOS_DEFAULT = 60
VENTANA_MINUTOS_MAX = 1440  # tope duro de la API (24h)

# Cuánto vive un pedido pre-registrado antes de darse por vencido. Nuestros
# reintentos se agotan mucho antes; el margen extra deja que un pago que llegó
# tarde igual concilie por webhook en vez de quedar huérfano.
EXPIRA_EN_MINUTOS_DEFAULT = 120

# Bancos que SendHook identifica por número de referencia. El resto (bfc,
# binance) no lo traen en el SMS/notificación y se desambiguan por
# `contraparte` — teléfono para BFC, nombre de quien envía para Binance.
BANCOS_CON_REFERENCIA = {"bdv", "bnc"}
BANCOS_SOPORTADOS = {"bdv", "bnc", "bfc", "binance"}


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.sendhook_api_url and settings.sendhook_api_key)


def is_webhook_configured() -> bool:
    return bool(get_settings().sendhook_webhook_secret)


def normalize_bank_label(value: str) -> Optional[str]:
    """
    Normaliza el texto libre del campo "Banco" (configurado por el admin en
    payment_method_values, ej. "BNC", "Banco de Venezuela") al slug que
    espera SendHook. Devuelve None si no es un banco que SendHook soporte hoy
    (ej. Banesco) — en ese caso la verificación automática se omite.
    """
    v = (value or "").strip().lower()
    if "bnc" in v or "nacional de cr" in v:
        return "bnc"
    if "bdv" in v or "de venezuela" in v:
        return "bdv"
    if "bfc" in v or "fondo com" in v:
        return "bfc"
    if "binance" in v:
        return "binance"
    return None


def _amount_token(amount_local: float) -> str:
    """
    Serializa el monto con 2 decimales fijos.

    El encoder JSON por default recorta los ceros de cola (25.50 -> "25.5",
    0.10 -> "0.1"). Formatear con f"{x:.2f}" y pegarlo como token numérico sin
    comillas produce JSON igual de válido, con siempre 2 decimales.
    """
    return f"{float(amount_local):.2f}"


def _build_body(monto: float, resto: dict) -> bytes:
    """Arma el JSON a mano para que `monto` conserve sus 2 decimales."""
    rest_json = json.dumps({k: v for k, v in resto.items() if v is not None})[1:]
    return f'{{"monto": {_amount_token(monto)}, {rest_json}'.encode("utf-8")


def build_identifiers(
    banco: str,
    reference_number: Optional[str],
    payer_phone: Optional[str],
    payer_name: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """
    Decide qué par (referencia, contraparte) mandarle a SendHook según el banco
    receptor. Devuelve (None, None) si no hay ningún dato con el que
    desambiguar — SendHook rechaza con 422 monto+banco solos, así que en ese
    caso ni conviene llamar.

    - BDV / BNC: mandan `referencia` y NADA de contraparte. Los dos filtros se
      combinan con AND, y estos bancos guardan el teléfono enmascarado
      ("0424***1486"), así que nuestro teléfono sin enmascarar nunca matchea
      como substring: agregarlo solo puede romper un match que ya era válido.
    - BFC: no trae referencia. Se identifica por el teléfono de quien pagó.
    - Binance: no trae referencia ni teléfono real. Se identifica por el
      nombre de quien envía en el P2P.
    """
    banco = (banco or "").strip().lower()
    referencia = (reference_number or "").strip() or None
    telefono = normalize_phone(payer_phone) if payer_phone else None
    nombre = (payer_name or "").strip() or None

    if banco in BANCOS_CON_REFERENCIA:
        if referencia:
            return referencia, None
        # Sin referencia sólo queda el teléfono, aunque venga enmascarado del
        # otro lado: es eso o no intentar la verificación.
        return None, telefono
    if banco == "binance":
        return None, nombre or telefono
    # BFC y cualquier otro banco por teléfono.
    return None, telefono or nombre


def normalize_phone(phone: str) -> str:
    """
    Normaliza a formato local venezolano 0XXXXXXXXXX (11 dígitos) para
    contraparte de SendHook. Toma siempre los últimos 10 dígitos (código de
    operadora + número, invariable sin importar el prefijo) y antepone "0" —
    esto tolera basura de prefijo real observada en producción (ej.
    "+5804243771486": el frontend concatena "+58" delante de un número que
    el usuario ya tipeó con su "0" local, dejando un 13-dígitos inválido que
    ningún prefijo fijo distingue de forma confiable).
    """
    nums = "".join(c for c in phone if c.isdigit())
    if len(nums) < 10:
        return nums
    return "0" + nums[-10:]


def _post(path: str, body: bytes) -> Optional[dict]:
    """POST autenticado. Devuelve el JSON si status 2xx, None si falla."""
    settings = get_settings()
    url = f"{settings.sendhook_api_url.rstrip('/')}{path}"
    try:
        import httpx

        logger.info("[sendhook] POST %s body=%s", url, body.decode("utf-8", "replace"))
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.post(
                url,
                content=body,
                headers={
                    "X-API-Key": settings.sendhook_api_key,
                    "Content-Type": "application/json",
                },
            )
        logger.info("[sendhook] POST %s -> %d", path, resp.status_code)
        if resp.status_code < 300:
            return resp.json()
        _log_api_error(path, resp.status_code, resp.text)
    except Exception as exc:
        logger.exception("[sendhook] POST %s falló: %s", path, exc)
    return None


def _get(path: str) -> Optional[dict]:
    settings = get_settings()
    url = f"{settings.sendhook_api_url.rstrip('/')}{path}"
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(url, headers={"X-API-Key": settings.sendhook_api_key})
        logger.info("[sendhook] GET %s -> %d", path, resp.status_code)
        if resp.status_code < 300:
            return resp.json()
        _log_api_error(path, resp.status_code, resp.text)
    except Exception as exc:
        logger.exception("[sendhook] GET %s falló: %s", path, exc)
    return None


def _log_api_error(path: str, status: int, text: str) -> None:
    """Traduce los códigos documentados a algo accionable en los logs."""
    if status == 401:
        logger.error(
            "[sendhook] 401 en %s — SENDHOOK_API_KEY inválida o empresa desactivada. %s",
            path, text,
        )
    elif status == 422:
        logger.error(
            "[sendhook] 422 en %s — payload incompleto (falta monto/banco, o ni "
            "referencia ni contraparte). %s", path, text,
        )
    elif status == 409:
        logger.info("[sendhook] 409 en %s — el pedido ya estaba conciliado. %s", path, text)
    elif status == 404:
        logger.info("[sendhook] 404 en %s — pedido inexistente para esta empresa.", path)
    else:
        logger.warning("[sendhook] %d en %s: %s", status, path, text)


# ---------------------------------------------------------------------------
# Verificación directa
# ---------------------------------------------------------------------------

def verificar_pago(
    monto: float,
    banco: str,
    referencia: Optional[str] = None,
    contraparte: Optional[str] = None,
    ventana_minutos: int = VENTANA_MINUTOS_DEFAULT,
) -> Optional[dict]:
    """
    POST /pagos/verificar. Devuelve el body de respuesta (siempre 200 si la
    llamada salió bien): `verificado` decide, y `motivo` explica el fallo
    (`no_encontrado`, `consumido`, `fuera_de_ventana`). Devuelve None si la
    llamada HTTP misma falló.
    """
    if not referencia and not contraparte:
        logger.info("[sendhook] verificar_pago sin referencia ni contraparte, se omite (daría 422).")
        return None
    body = _build_body(monto, {
        "banco": banco,
        "referencia": referencia,
        "contraparte": contraparte,
        "ventana_minutos": min(max(int(ventana_minutos), 1), VENTANA_MINUTOS_MAX),
    })
    return _post("/pagos/verificar", body)


# ---------------------------------------------------------------------------
# Pedidos (pre-registro + webhook)
# ---------------------------------------------------------------------------

def registrar_pedido(
    referencia_externa: str,
    monto: float,
    banco: str,
    referencia: Optional[str] = None,
    contraparte: Optional[str] = None,
    expira_en_minutos: int = EXPIRA_EN_MINUTOS_DEFAULT,
) -> Optional[dict]:
    """
    POST /pedidos. Deja anotado el pago que esperamos; SendHook nos avisa por
    webhook cuando llegue.

    `referencia_externa` es el id de NUESTRO pago y funciona como clave de
    idempotencia: reintentar con el mismo id devuelve el pedido existente sin
    pisar nada. Si el pago ya había llegado antes de registrarlo, la respuesta
    viene directamente con estado "conciliado" y el objeto `pago` lleno.
    """
    if not referencia and not contraparte:
        logger.info("[sendhook] registrar_pedido sin referencia ni contraparte, se omite (daría 422).")
        return None
    body = _build_body(monto, {
        "referencia_externa": referencia_externa,
        "banco": banco,
        "referencia": referencia,
        "contraparte": contraparte,
        "expira_en_minutos": min(max(int(expira_en_minutos), 1), VENTANA_MINUTOS_MAX),
    })
    return _post("/pedidos", body)


def consultar_pedido(referencia_externa: str) -> Optional[dict]:
    """GET /pedidos/{ref}. El estado real en cualquier momento, por si el webhook no llegó."""
    return _get(f"/pedidos/{referencia_externa}")


def cancelar_pedido(referencia_externa: str) -> Optional[dict]:
    """
    POST /pedidos/{ref}/cancelar. Libera un pedido que ya no esperamos (el
    admin rechazó el pago a mano). Un pedido ya conciliado responde 409 y no
    se cancela — eso es correcto y no es un error nuestro.
    """
    return _post(f"/pedidos/{referencia_externa}/cancelar", b"{}")


# ---------------------------------------------------------------------------
# Webhook entrante
# ---------------------------------------------------------------------------

def verificar_firma_webhook(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """
    Valida `X-SendHook-Signature` sobre el cuerpo CRUDO.

    La firma es HMAC-SHA256 de "{timestamp}.{cuerpo_crudo}" con el secreto
    whsec_. Tiene que calcularse byte por byte como llegó: si se parsea el
    JSON y se vuelve a serializar, el orden de las claves cambia y no coincide.
    """
    secret = get_settings().sendhook_webhook_secret
    if not secret or not timestamp or not signature:
        return False

    try:
        emitido_en = int(timestamp)
    except (TypeError, ValueError):
        logger.warning("[sendhook.webhook] timestamp no numérico: %r", timestamp)
        return False

    # Anti-replay: si alguien capturó un evento viejo y lo reenvía, el
    # timestamp ya no cuadra (va firmado, así que no lo pueden cambiar).
    if abs(time.time() - emitido_en) > WEBHOOK_TOLERANCE_SECONDS:
        logger.warning("[sendhook.webhook] timestamp fuera de rango (%s).", timestamp)
        return False

    esperada = "v1=" + hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()

    # compare_digest y no ==: comparar strings normal tarda distinto según
    # cuántos caracteres coinciden, y eso deja adivinar la firma de a poco.
    return hmac.compare_digest(esperada, signature)

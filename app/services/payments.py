import base64
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.core.deps import invalidate_profile_cache
from app.core.exceptions import supabase_error
from app.core.rate_limit import check_user_rate_limit
from app.core.supabase import get_supabase
from app.services import plans as plans_service
from app.services import sendhook

logger = logging.getLogger(__name__)

_RECEIPT_BUCKET = "receipts"
_SAFE_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]")

def _get_destination_bank_slug(supabase, payment_method_id: str) -> Optional[str]:
    """
    Cuenta receptora de El Club de Nice para este método de pago (a qué banco
    le está escuchando el teléfono de SendHook) — NO el banco emisor que
    eligió el pagador. Se lee del campo cuyo field_label es "Banco" en
    payment_method_fields/payment_method_values (el mismo mecanismo que ya
    usa el admin para mostrarle al usuario los datos de pago).
    """
    try:
        fields_resp = (
            supabase.table("payment_method_fields")
            .select("id, field_label")
            .eq("payment_method_id", payment_method_id)
            .execute()
        )
    except Exception as exc:
        logger.warning("[_get_destination_bank_slug] fields lookup failed method_id=%s [%s] %s", payment_method_id, type(exc).__name__, supabase_error(exc))
        return None

    field = next((f for f in (fields_resp.data or []) if f["field_label"].strip().lower() == "banco"), None)
    if not field:
        return None

    try:
        value_resp = (
            supabase.table("payment_method_values")
            .select("value")
            .eq("payment_method_field_id", field["id"])
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        logger.warning("[_get_destination_bank_slug] value lookup failed field_id=%s [%s] %s", field["id"], type(exc).__name__, supabase_error(exc))
        return None

    value = (value_resp.data or {}).get("value")
    return sendhook.normalize_bank_label(value) if value else None


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

def _sanitize_path_segment(value: str) -> str:
    """Evita path traversal en rutas de Storage construidas con input público."""
    cleaned = _SAFE_SEGMENT_RE.sub("_", value.strip()).strip("._")
    if not cleaned:
        raise HTTPException(status_code=400, detail="Valor inválido para construir la ruta del archivo.")
    return cleaned


def _get_user_email(supabase, user_id: str) -> Optional[str]:
    try:
        resp = supabase.auth.admin.get_user_by_id(user_id)
        return resp.user.email if resp.user else None
    except Exception as exc:
        logger.warning("[_get_user_email] lookup failed user_id=%s [%s] %s", user_id, type(exc).__name__, supabase_error(exc))
        return None


def _get_profile_name(supabase, user_id: str) -> Optional[str]:
    try:
        resp = supabase.table("profiles").select("name").eq("id", user_id).maybe_single().execute()
        return (resp.data or {}).get("name")
    except Exception as exc:
        logger.warning("[payments._get_profile_name] lookup failed user_id=%s [%s] %s", user_id, type(exc).__name__, supabase_error(exc))
        return None


def _is_admin(supabase, user_id: str) -> bool:
    try:
        result = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
        return bool(result.data) and result.data.get("role") == "admin"
    except Exception:
        return False


def _get_payment_or_404(supabase, payment_id: str) -> dict:
    try:
        result = supabase.table("payments").select("*").eq("id", payment_id).maybe_single().execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments._get_payment_or_404] FAILED id=%s [%s] %s", payment_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    # `maybe_single()` devuelve None sin filas, no un objeto con `.data` vacío.
    if result is None or not result.data:
        raise HTTPException(status_code=404, detail="Pago no encontrado.")
    return result.data


def _cleanup_failed_registration(supabase, user_id: str) -> None:
    """Revierte la creación del usuario cuando falla un paso posterior del registro con pago."""
    try:
        supabase.table("profiles").delete().eq("id", user_id).execute()
    except Exception as exc:
        logger.warning("[payments._cleanup] profile cleanup failed user_id=%s [%s] %s", user_id, type(exc).__name__, supabase_error(exc))
    try:
        supabase.auth.admin.delete_user(user_id)
    except Exception as exc:
        logger.warning("[payments._cleanup] auth user cleanup failed user_id=%s [%s] %s", user_id, type(exc).__name__, supabase_error(exc))


# Reintentos en background tras el primer intento fallido, por si el SMS/
# notificación todavía no había llegado a SendHook. Los primeros 3 (20s,
# 30s, 30s) cubren el caso rápido; el último (300s = 5 min) le da margen
# real al teléfono/app de SendHook para procesar la notificación en casos
# más lentos antes de rendirse y dejar el pago "pending" para revisión manual.
#
# Son una RED DE SEGURIDAD: el camino principal es el webhook de SendHook
# (ver app/api/sendhook.py), que suele llegar antes que el primer reintento.
# Se mantienen porque el webhook depende de que el admin de SendHook haya
# registrado nuestra URL, y porque un webhook puede perderse.
_RETRY_DELAYS_SECONDS = [20, 30, 30, 300]


def _payment_is_still_pending(supabase, payment_id: str) -> Optional[bool]:
    """
    True/False según el estado en DB; None si la consulta falló.

    La lectura del resultado va dentro del try a propósito: con
    `maybe_single()` supabase-py devuelve None (no un objeto con `.data`
    vacío) cuando no hay fila, así que tocar `.data` afuera revienta con
    AttributeError justo en el caso normal de "ese pago no existe" — que es
    exactamente lo que llega si el webhook trae una referencia_externa
    desconocida.
    """
    try:
        current = supabase.table("payments").select("status").eq("id", payment_id).maybe_single().execute()
        if not current or not current.data:
            return False
        return current.data.get("status") == "pending"
    except Exception as exc:
        logger.warning("[verify_auto] status check FAILED payment_id=%s [%s] %s", payment_id, type(exc).__name__, supabase_error(exc))
        return None


def approve_from_sendhook(payment_id: str, pago: Optional[dict], source: str) -> bool:
    """
    Aprueba un pago que SendHook dio por bueno, venga del webhook o de un
    reintento. Devuelve True si lo aprobó en esta llamada.

    Es idempotente por diseño: si el pago ya no está 'pending' (un admin lo
    aprobó a mano, o el webhook nos ganó de mano al reintento) no hace nada y
    devuelve False, en vez de dejar que approve_payment tire un 400.
    """
    supabase = get_supabase()
    if _payment_is_still_pending(supabase, payment_id) is not True:
        logger.info("[verify_auto.%s] payment_id=%s ya no está pending, no se aprueba de nuevo.", source, payment_id)
        return False

    try:
        approve_payment(payment_id)
    except HTTPException as exc:
        logger.warning("[verify_auto.%s] approve_payment falló payment_id=%s: %s", source, payment_id, exc.detail)
        return False

    # `pago_id` es el identificador definitivo del pago del lado de SendHook:
    # guardarlo da trazabilidad pago-a-pedido (imprescindible con Binance, donde
    # dos pagos iguales del mismo cliente sólo se distinguen por id y fecha).
    _store_sendhook_payment_id(supabase, payment_id, pago)
    return True


def _store_sendhook_payment_id(supabase, payment_id: str, pago: Optional[dict]) -> None:
    """Best-effort: si la columna todavía no existe en la DB, se ignora."""
    pago_id = (pago or {}).get("id") or (pago or {}).get("pago_id")
    if pago_id is None:
        return
    try:
        supabase.table("payments").update({"sendhook_payment_id": pago_id}).eq("id", payment_id).execute()
    except Exception as exc:
        logger.info(
            "[verify_auto] No se pudo guardar sendhook_payment_id=%s en payment_id=%s (¿falta la columna?): %s",
            pago_id, payment_id, supabase_error(exc),
        )


def _retry_verification_in_background(
    payment_id: str, amount_local: float, sendhook_bank: str,
    referencia: Optional[str], contraparte: Optional[str], pedido_registrado: bool,
) -> None:
    """
    Red de seguridad por si el webhook no llega. Hilo daemon, no bloquea la
    request original. Antes de cada intento chequea que el pago siga 'pending'
    — si el webhook o un admin ya lo resolvió, corta.

    Si el pedido quedó pre-registrado en SendHook, se consulta
    `GET /pedidos/{id}` (barato y no consume ningún pago). Si no, se cae a
    `/pagos/verificar`, interpretando el `motivo` del fallo:

    - `consumido`: ese pago ya se usó para aprobar otra cosa. Reintentar no lo
      va a cambiar nunca, así que se corta y queda para revisión manual.
    - `fuera_de_ventana`: existe un pago que encaja pero es más viejo que la
      ventana pedida. Se amplía la ventana al tope de 24h y se sigue.
    - `no_encontrado`: todavía no llegó (o nunca va a llegar). Se sigue
      reintentando, que es exactamente para lo que están los reintentos.
    """
    supabase = get_supabase()
    ventana = sendhook.VENTANA_MINUTOS_DEFAULT

    for delay in _RETRY_DELAYS_SECONDS:
        time.sleep(delay)

        if _payment_is_still_pending(supabase, payment_id) is not True:
            logger.info("[verify_auto.retry] payment_id=%s ya no está pending, deteniendo reintentos.", payment_id)
            return

        if pedido_registrado:
            pedido = sendhook.consultar_pedido(payment_id)
            if pedido and pedido.get("estado") == "conciliado":
                logger.info("[verify_auto.retry] Pedido conciliado en SendHook. Aprobando payment_id=%s", payment_id)
                approve_from_sendhook(payment_id, pedido.get("pago"), "retry")
                return
            if pedido and pedido.get("estado") in ("cancelado", "expirado"):
                logger.info(
                    "[verify_auto.retry] Pedido en estado '%s' para payment_id=%s, queda para revisión manual.",
                    pedido.get("estado"), payment_id,
                )
                return
            continue

        data = sendhook.verificar_pago(amount_local, sendhook_bank, referencia, contraparte, ventana)
        if data and data.get("verificado") is True:
            logger.info(
                "[verify_auto.retry] Pago verificado por SendHook en reintento (pago_id=%s). Aprobando payment_id=%s",
                data.get("pago_id"), payment_id,
            )
            approve_from_sendhook(payment_id, data, "retry")
            return

        motivo = (data or {}).get("motivo")
        if motivo == "consumido":
            logger.warning(
                "[verify_auto.retry] payment_id=%s: SendHook reporta 'consumido' (ese pago ya aprobó "
                "otro pedido). Se cortan los reintentos, queda para revisión manual.", payment_id,
            )
            return
        if motivo == "fuera_de_ventana" and ventana < sendhook.VENTANA_MINUTOS_MAX:
            logger.info(
                "[verify_auto.retry] payment_id=%s: 'fuera_de_ventana', ampliando la ventana a %d minutos.",
                payment_id, sendhook.VENTANA_MINUTOS_MAX,
            )
            ventana = sendhook.VENTANA_MINUTOS_MAX

    logger.info("[verify_auto.retry] payment_id=%s sin match tras los reintentos, queda pending para revisión manual.", payment_id)


def _verify_payment_automatically(
    payment_id: str,
    method_auto_verify: bool,
    reference_number: str,
    phone: str,
    amount_local: float,
    sendhook_bank: Optional[str],
    payer_phone: Optional[str] = None,
    payer_name: Optional[str] = None,
) -> Optional[dict]:
    """
    Intenta verificar un pago móvil automáticamente contra SendHook.

    Camino principal (el que SendHook recomienda): se registra el pago que
    esperamos con POST /pedidos, usando NUESTRO payment_id como
    `referencia_externa` — que además es la clave de idempotencia del lado de
    ellos. Dos desenlaces:

    - El pago ya había llegado: la respuesta viene con estado "conciliado" y
      se aprueba en el acto, devolviendo el registro aprobado.
    - Todavía no llegó: queda "pendiente" y SendHook nos avisa por webhook
      (app/api/sendhook.py) en cuanto entre. La request no espera nada.

    Como red de seguridad se agenda igual un hilo de reintentos, que consulta
    GET /pedidos/{id} por si el webhook no está configurado o se pierde.

    Si el pre-registro falla (SendHook caído, 422), se cae al camino viejo de
    POST /pagos/verificar + reintentos.

    `sendhook_bank` es el banco RECEPTOR (la cuenta de El Club de Nice a la
    que el teléfono con la app SendHook está escuchando) — no el banco emisor
    que eligió el pagador. Se resuelve antes de llamar a esta función vía
    `_get_destination_bank_slug()`.
    """
    if not sendhook.is_configured():
        logger.info("[verify_auto] SendHook no configurado, omitiendo verificación.")
        return None

    if not method_auto_verify:
        logger.info("[verify_auto] Método sin auto_verify=True, omitiendo.")
        return None

    if not sendhook_bank:
        logger.info("[verify_auto] No se pudo resolver el banco receptor (campo 'Banco' del método), omitiendo.")
        return None

    # Qué dato identifica el pago depende del banco receptor: BDV/BNC traen
    # referencia, BFC se identifica por teléfono y Binance por el nombre de
    # quien envía. Mandar el dato equivocado garantiza un no_encontrado.
    referencia, contraparte = sendhook.build_identifiers(
        sendhook_bank, reference_number, payer_phone or phone, payer_name,
    )
    if not referencia and not contraparte:
        logger.info(
            "[verify_auto] payment_id=%s sin referencia ni contraparte para banco=%s, omitiendo "
            "(la API rechaza monto+banco solos).", payment_id, sendhook_bank,
        )
        return None

    logger.info(
        "[verify_auto] Iniciando para payment_id=%s banco=%s referencia=%s contraparte=%s",
        payment_id, sendhook_bank, referencia, contraparte,
    )

    pedido = sendhook.registrar_pedido(
        referencia_externa=str(payment_id),
        monto=amount_local,
        banco=sendhook_bank,
        referencia=referencia,
        contraparte=contraparte,
    )

    if pedido and pedido.get("estado") == "conciliado":
        logger.info("[verify_auto] El pago ya había llegado: pedido conciliado. Aprobando payment_id=%s", payment_id)
        if approve_from_sendhook(payment_id, pedido.get("pago"), "pedido"):
            return _get_payment_or_404(get_supabase(), payment_id)
        return None

    pedido_registrado = bool(pedido)
    if not pedido_registrado:
        # SendHook no aceptó el pre-registro. Intentamos la verificación
        # directa acá mismo por si el pago ya estaba disponible.
        data = sendhook.verificar_pago(amount_local, sendhook_bank, referencia, contraparte)
        if data and data.get("verificado") is True:
            logger.info(
                "[verify_auto] Pago verificado por SendHook (pago_id=%s). Aprobando payment_id=%s",
                data.get("pago_id"), payment_id,
            )
            if approve_from_sendhook(payment_id, data, "verificar"):
                return _get_payment_or_404(get_supabase(), payment_id)
            return None
        if (data or {}).get("motivo") == "consumido":
            logger.warning(
                "[verify_auto] payment_id=%s: SendHook reporta 'consumido'. No se agendan reintentos.", payment_id,
            )
            return None
        if not sendhook.is_webhook_configured():
            logger.warning(
                "[verify_auto] Ni pre-registro de pedido ni SENDHOOK_WEBHOOK_SECRET configurado: "
                "el pago %s depende sólo de los reintentos.", payment_id,
            )

    logger.info(
        "[verify_auto] payment_id=%s queda esperando (pedido_registrado=%s); se agendan reintentos de respaldo.",
        payment_id, pedido_registrado,
    )
    threading.Thread(
        target=_retry_verification_in_background,
        args=(payment_id, amount_local, sendhook_bank, referencia, contraparte, pedido_registrado),
        daemon=True,
    ).start()

    return None


# Cuántas consultas manuales puede hacer un usuario por minuto. El botón está
# para que alguien que acaba de pagar no tenga que esperar al reintento de los
# 5 minutos, no para sondear en bucle: cada pulsación es una llamada a
# SendHook y ellos ven el tráfico como nuestro.
_RECHECK_MAX_PER_MINUTE = 4


def recheck_pending_payment(user_id: str) -> dict:
    """
    Re-consulta a SendHook el último pago pendiente del usuario, a pedido suyo
    (botón "Verificar estado"). Si el pago ya entró, lo aprueba en el acto.

    Es el mismo chequeo que hace el hilo de reintentos, pero disparado a mano:
    sirve para el caso normal de alguien que paga, se registra, y no quiere
    esperar a que caiga el reintento siguiente. Está limitado por usuario para
    que no se convierta en un sondeo continuo contra SendHook.

    Returns:
        {"verificado": bool, "estado": str, "message": str, "payment": dict|None}
        `estado` es uno de: "aprobado", "esperando", "sin_pendiente",
        "no_automatico", "requiere_revision".
    Raises:
        HTTPException 429 — demasiadas consultas seguidas del mismo usuario.
    """
    check_user_rate_limit(user_id, _RECHECK_MAX_PER_MINUTE, 60, "payments-recheck")

    supabase = get_supabase()

    try:
        resp = (
            supabase.table("payments")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.recheck] lookup FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = resp.data or []
    if not rows:
        # Puede ser que ya se lo aprobaran (por webhook, por un reintento o a
        # mano) entre que cargó la pantalla y pulsó el botón.
        return {
            "verificado": False,
            "estado": "sin_pendiente",
            "message": "No tienes ningún pago esperando verificación.",
            "payment": None,
        }

    payment = rows[0]
    payment_id = payment["id"]

    if not sendhook.is_configured():
        return {
            "verificado": False,
            "estado": "no_automatico",
            "message": "Tu pago lo revisará un administrador. Te avisaremos por correo en cuanto se apruebe.",
            "payment": payment,
        }

    sendhook_bank = _get_destination_bank_slug(supabase, payment.get("payment_method_id")) if payment.get("payment_method_id") else None
    referencia, contraparte = sendhook.build_identifiers(
        sendhook_bank or "",
        payment.get("reference_number"),
        payment.get("payer_phone") or payment.get("phone"),
        _get_profile_name(supabase, user_id),
    )

    if not sendhook_bank or (not referencia and not contraparte):
        # Método sin verificación automática posible (banco que SendHook no
        # soporta, o sin ningún dato con el que desambiguar).
        return {
            "verificado": False,
            "estado": "no_automatico",
            "message": "Tu pago lo revisará un administrador. Te avisaremos por correo en cuanto se apruebe.",
            "payment": payment,
        }

    logger.info("[payments.recheck] user_id=%s payment_id=%s banco=%s", user_id, payment_id, sendhook_bank)

    # 1) Si el pedido quedó pre-registrado, preguntarle a él: es la consulta
    #    barata y no consume ningún pago del lado de SendHook.
    pedido = sendhook.consultar_pedido(str(payment_id))
    if pedido and pedido.get("estado") == "conciliado":
        if approve_from_sendhook(payment_id, pedido.get("pago"), "recheck"):
            return _recheck_approved(supabase, payment_id)

    # 2) Si no hay pedido (o sigue pendiente), preguntar directo por el pago.
    #    Se usa la ventana máxima: acá el usuario está diciendo "ya pagué", y
    #    un pago de hace horas es exactamente el caso que hay que rescatar.
    amount_local = payment.get("amount_local")
    if amount_local is None:
        return _recheck_waiting(payment, None)

    data = sendhook.verificar_pago(
        float(amount_local), sendhook_bank, referencia, contraparte,
        ventana_minutos=sendhook.VENTANA_MINUTOS_MAX,
    )

    if data and data.get("verificado") is True:
        logger.info("[payments.recheck] Verificado (pago_id=%s). Aprobando payment_id=%s", data.get("pago_id"), payment_id)
        if approve_from_sendhook(payment_id, data, "recheck"):
            return _recheck_approved(supabase, payment_id)
        # Alguien lo aprobó entre medio; devolver el estado real igual.
        return _recheck_approved(supabase, payment_id)

    return _recheck_waiting(payment, (data or {}).get("motivo"))


def _recheck_approved(supabase, payment_id: str) -> dict:
    return {
        "verificado": True,
        "estado": "aprobado",
        "message": "¡Pago verificado! Tu cuenta ya está activa.",
        "payment": _get_payment_or_404(supabase, payment_id),
    }


def _recheck_waiting(payment: dict, motivo: Optional[str]) -> dict:
    """Traduce el `motivo` de SendHook a algo que le sirva a quien está mirando la pantalla."""
    if motivo == "consumido":
        # Concluyente sólo cuando mandamos referencia (BDV/BNC). Reintentar no
        # lo va a cambiar, así que se le dice que hable con un administrador en
        # vez de dejarlo pulsando el botón.
        return {
            "verificado": False,
            "estado": "requiere_revision",
            "message": (
                "Ese pago ya figura usado en otra activación. Un administrador tiene que revisarlo; "
                "escríbenos con tu número de referencia."
            ),
            "payment": payment,
        }
    if motivo == "fuera_de_ventana":
        return {
            "verificado": False,
            "estado": "requiere_revision",
            "message": (
                "Encontramos un pago parecido pero es más antiguo de lo que podemos validar solos. "
                "Un administrador lo revisará."
            ),
            "payment": payment,
        }
    return {
        "verificado": False,
        "estado": "esperando",
        "message": (
            "Todavía no vemos tu pago. Suele tardar unos minutos desde que el banco lo notifica; "
            "vuelve a intentarlo en un momento."
        ),
        "payment": payment,
    }


# ---------------------------------------------------------------------------
# Registro con pago
# ---------------------------------------------------------------------------


def register_with_payment(
    name: str, email: str, password: str, plan: str, amount: float,
    payment_method_id: str, reference_number: str, phone: str, receipt_path: str,
    currency_id: str, amount_local: float, exchange_rate: float,
    origin_bank: Optional[str] = None, payer_id_number: Optional[str] = None,
    payer_phone: Optional[str] = None, payment_date: Optional[str] = None,
) -> dict:
    """
    Crea el usuario en Supabase Auth + perfil (role='miembro', subscription_status='inactive')
    + registro de pago en estado 'pending' (y opcionalmente intenta la verificación automática).

    Returns:
        {"user": {...}, "payment": {...}}
    Raises:
        HTTPException 400 — email ya registrado, método de pago inválido/inactivo u otro error de Supabase Auth
        HTTPException 500 — fallo creando el perfil o el registro de pago (revierte lo creado)
    """
    logger.info("[payments.register] start - email=%s plan=%s", email, plan)
    supabase = get_supabase()

    # 0. Validar que el plan y el método de pago existan y estén activos
    plans_service.validate_active_plan(supabase, plan)
    try:
        method_resp = (
            supabase.table("payment_methods")
            .select("id, name, is_active, auto_verify")
            .eq("id", payment_method_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.register] step 0/3 FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    method = method_resp.data
    if not method or not method.get("is_active"):
        raise HTTPException(status_code=400, detail="El método de pago seleccionado no está disponible.")

    # 1. Crear usuario en Supabase Auth
    try:
        auth_resp = supabase.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
        })
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.register] step 1/3 FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        msg_lower = msg.lower()
        if "already registered" in msg_lower or "already been registered" in msg_lower:
            raise HTTPException(status_code=400, detail="Este email ya está registrado. Intenta iniciar sesión.")
        raise HTTPException(status_code=400, detail=f"Error al crear usuario en Supabase: {msg}")

    user_id = auth_resp.user.id
    avatar = ""  # Sin imagen — el frontend muestra la inicial del nombre
    logger.info("[payments.register] step 1/3 OK - user_id=%s", user_id)

    # 2. Insertar perfil con acceso inactivo hasta que se apruebe el pago
    try:
        supabase.table("profiles").insert({
            "id": user_id,
            "name": name,
            "role": "miembro",
            "avatar": avatar,
            "bio": "",
            "subscription_status": "inactive",
        }).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.register] step 2/3 FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        _cleanup_failed_registration(supabase, user_id)
        raise HTTPException(status_code=500, detail=f"Error al crear perfil: {msg}")

    logger.info("[payments.register] step 2/3 OK")

    # 3. Insertar el pago en estado pendiente de revisión (con fallback defensivo por si faltan columnas en la DB)
    insert_data = {
        "user_id": user_id,
        "plan": plan,
        "amount": amount,
        "status": "pending",
        "payment_method_id": payment_method_id,
        "payment_method": method["name"],
        "reference_number": reference_number,
        "receipt_url": receipt_path,
        "phone": phone,
        "currency_id": currency_id,
        "amount_local": amount_local,
        "exchange_rate": exchange_rate,
        "origin_bank": origin_bank,
        "payer_id_number": payer_id_number,
        "payer_phone": payer_phone,
        "payment_date": payment_date,
    }
    try:
        payment_resp = supabase.table("payments").insert(insert_data).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        if "payer_phone" in msg or "payment_date" in msg:
            logger.warning("[payments.register] Faltan columnas en DB. Reintentando sin payer_phone/payment_date. Error: %s", msg)
            insert_data.pop("payer_phone", None)
            insert_data.pop("payment_date", None)
            try:
                payment_resp = supabase.table("payments").insert(insert_data).execute()
            except Exception as retry_exc:
                msg_retry = supabase_error(retry_exc)
                logger.error("[payments.register] Reintento de inserción falló: %s", msg_retry)
                _cleanup_failed_registration(supabase, user_id)
                raise HTTPException(status_code=500, detail=f"Error al registrar el pago: {msg_retry}")
        else:
            logger.error("[payments.register] step 3/3 FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
            _cleanup_failed_registration(supabase, user_id)
            raise HTTPException(status_code=500, detail=f"Error al registrar el pago: {msg}")

    payment = payment_resp.data[0]
    logger.info("[payments.register] OK - user_id=%s payment_id=%s", user_id, payment["id"])

    # Intentar la verificación automática del pago
    is_auto_verify = method.get("auto_verify", False)
    sendhook_bank = _get_destination_bank_slug(supabase, payment_method_id) if is_auto_verify else None

    approved_payment = _verify_payment_automatically(
        payment["id"], is_auto_verify, reference_number, phone,
        amount_local, sendhook_bank, payer_phone,
        # Binance P2P no reporta referencia ni teléfono: el nombre de quien
        # envía es el único dato con el que SendHook puede desambiguar.
        payer_name=name,
    )

    if approved_payment:
        # Si se aprobó de forma automática, el estado de suscripción del perfil ya es 'active'
        # gracias al trigger de Supabase, pero lo devolvemos explícitamente al cliente
        return {
            "user": {
                "id": user_id, "name": name, "email": email, "role": "miembro",
                "avatar": avatar, "bio": "", "subscription_status": "active",
            },
            "payment": approved_payment,
            "message": "¡Pago verificado automáticamente! Tu cuenta ha sido activada de inmediato.",
        }

    return {
        "user": {
            "id": user_id, "name": name, "email": email, "role": "miembro",
            "avatar": avatar, "bio": "", "subscription_status": "inactive",
        },
        "payment": payment,
        "message": "Registro recibido. Tu pago está en revisión, te notificaremos cuando sea aprobado.",
    }


# ---------------------------------------------------------------------------
# Comprobantes
# ---------------------------------------------------------------------------

def upload_receipt(reference_number: str, filename: str, file_data: str) -> dict:
    """
    Sube el comprobante de pago al bucket `receipts` (público, sin auth) bajo
    la ruta `{referencia}/{filename}`.

    Returns:
        {"path": "..."}
    Raises:
        HTTPException 400 — formato de archivo inválido o segmentos de ruta vacíos
        HTTPException 500 — fallo al subir a Supabase Storage
    """
    logger.info("[payments.upload_receipt] reference_number=%s filename=%s", reference_number, filename)

    match = re.match(r"^data:(.+);base64,(.+)$", file_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de archivo inválido")

    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    path = f"{_sanitize_path_segment(reference_number)}/{_sanitize_path_segment(filename)}"

    supabase = get_supabase()
    try:
        supabase.storage.from_(_RECEIPT_BUCKET).upload(
            path, raw_bytes,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.upload_receipt] upload FAILED path=%s [%s] %s", path, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error subiendo el comprobante: {msg}")

    logger.info("[payments.upload_receipt] OK path=%s", path)
    return {"path": path}


def get_receipt_signed_url(payment_id: str) -> dict:
    """
    Genera una signed URL temporal (1 hora) para que un admin vea el comprobante.

    Returns:
        {"url": "...", "expires_in": 3600}
    Raises:
        HTTPException 404 — pago no encontrado o sin comprobante adjunto
        HTTPException 500 — fallo generando la signed URL
    """
    logger.info("[payments.get_receipt_signed_url] payment_id=%s", payment_id)
    supabase = get_supabase()
    payment = _get_payment_or_404(supabase, payment_id)

    receipt_path = payment.get("receipt_url")
    if not receipt_path:
        raise HTTPException(status_code=404, detail="Este pago no tiene comprobante adjunto.")

    try:
        signed = supabase.storage.from_(_RECEIPT_BUCKET).create_signed_url(receipt_path, 3600)
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.get_receipt_signed_url] FAILED path=%s [%s] %s", receipt_path, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"No se pudo generar la URL del comprobante: {msg}")

    url = signed.get("signedURL") or signed.get("signedUrl")
    if not url:
        raise HTTPException(status_code=500, detail="No se pudo generar la URL del comprobante.")

    logger.info("[payments.get_receipt_signed_url] OK payment_id=%s", payment_id)
    return {"url": url, "expires_in": 3600}


# ---------------------------------------------------------------------------
# Listado y consulta
# ---------------------------------------------------------------------------

def list_payments() -> list:
    """
    Admin — lista todos los pagos ordenados por fecha de creación desc, con
    el nombre y email del usuario asociado.

    Returns:
        Lista de pagos, cada uno con `user_name` y `user_email` añadidos.
    Raises:
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payments.list] fetching all")
    supabase = get_supabase()

    try:
        result = (
            supabase.table("payments")
            .select("*, profiles(name)")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.list] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = result.data or []
    email_cache: dict[str, Optional[str]] = {}
    for row in rows:
        profile = row.pop("profiles", None) or {}
        row["user_name"] = profile.get("name")

        user_id = row.get("user_id")
        # user_id puede ser NULL: pagos de registro rechazados cuya cuenta
        # se eliminó, desvinculados a propósito (ver reject_payment) para
        # quedar como registro histórico sin apuntar a un usuario inexistente.
        if user_id and user_id not in email_cache:
            email_cache[user_id] = _get_user_email(supabase, user_id)
        row["user_email"] = email_cache.get(user_id)

    logger.info("[payments.list] returned %d items", len(rows))
    return rows


def get_user_payments(user_id: str, requester_id: str) -> list:
    """
    Devuelve el historial de pagos de `user_id`. Permitido para el propio
    usuario o para un admin.

    Returns:
        Lista de pagos del usuario ordenados por fecha de creación desc.
    Raises:
        HTTPException 403 — el solicitante no es ni el dueño ni un admin
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payments.get_user_payments] user_id=%s requester_id=%s", user_id, requester_id)
    supabase = get_supabase()

    if requester_id != user_id and not _is_admin(supabase, requester_id):
        raise HTTPException(status_code=403, detail="No tienes permiso para ver estos pagos.")

    try:
        result = (
            supabase.table("payments")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.get_user_payments] FAILED user_id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    rows = result.data or []
    logger.info("[payments.get_user_payments] returned %d items", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Aprobación / rechazo (admin)
# ---------------------------------------------------------------------------

def _compute_expires_at(supabase, plan: str, from_dt: datetime) -> Optional[str]:
    duration_days = plans_service.get_plan_duration_days(supabase, plan)
    if duration_days is None:
        return None
    return (from_dt + timedelta(days=duration_days)).isoformat()


def approve_payment(payment_id: str) -> dict:
    """
    Admin aprueba un pago: status -> 'success', paid_at = now(), y calcula
    expires_at según el plan (None si es indefinido). El trigger de Supabase
    se encarga de actualizar profiles.subscription_status -> 'active'.

    Returns:
        El registro de pago actualizado.
    Raises:
        HTTPException 404 — pago no encontrado
        HTTPException 400 — el pago ya fue procesado anteriormente
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payments.approve] payment_id=%s", payment_id)
    supabase = get_supabase()
    payment = _get_payment_or_404(supabase, payment_id)

    if payment["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Este pago ya fue procesado (estado actual: {payment['status']}).")

    now = datetime.now(timezone.utc)
    expires_at = _compute_expires_at(supabase, payment["plan"], now)

    try:
        result = (
            supabase.table("payments")
            .update({"status": "success", "paid_at": now.isoformat(), "expires_at": expires_at})
            .eq("id", payment_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.approve] update FAILED id=%s [%s] %s", payment_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not result.data:
        raise HTTPException(status_code=500, detail="No se pudo actualizar el pago.")

    invalidate_profile_cache(payment["user_id"])
    logger.info("[payments.approve] OK payment_id=%s expires_at=%s", payment_id, expires_at)

    approved = result.data[0]
    # Fire-and-forget: enviar correo de bienvenida / confirmación de pago
    try:
        from app.services import email as email_service
        user_email = _get_user_email(supabase, payment["user_id"])
        profile_resp = supabase.table("profiles").select("name").eq("id", payment["user_id"]).maybe_single().execute()
        user_name = (profile_resp.data or {}).get("name") or "miembro"
        if user_email:
            email_service.send_payment_approved(user_email, user_name, approved.get("plan", ""), approved.get("expires_at"))
    except Exception as exc:
        logger.warning("[payments.approve] welcome email failed: %s", exc)

    return approved


def reject_payment(payment_id: str) -> dict:
    """
    Admin rechaza un pago: status -> 'failed'. El trigger de Supabase deja
    profiles.subscription_status como corresponda (no se modifica manualmente).

    Si este era el ÚNICO pago que tuvo el usuario (nunca tuvo uno aprobado
    antes), significa que era su pago de registro — la cuenta se elimina
    (perfil + usuario de Auth) para liberar el email y que la persona pueda
    volver a registrarse con datos corregidos. La fila de payments NO se
    borra: se desvincula (user_id -> NULL) y queda como registro histórico
    en el panel admin (referencia/monto/teléfono siguen visibles, para
    detectar abuso o resolver reclamos), ya sin ligarla a ninguna cuenta.
    Si el usuario ya tenía otros pagos (ej. una renovación rechazada de una
    cuenta activa o expirada), la cuenta NUNCA se toca.

    Returns:
        El registro de pago actualizado (aunque la cuenta se haya eliminado).
    Raises:
        HTTPException 404 — pago no encontrado
        HTTPException 400 — el pago ya fue procesado anteriormente
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payments.reject] payment_id=%s", payment_id)
    supabase = get_supabase()
    payment = _get_payment_or_404(supabase, payment_id)

    if payment["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Este pago ya fue procesado (estado actual: {payment['status']}).")

    # El admin descartó este pago: si había un pedido esperándolo en SendHook,
    # se cancela para que no siga vivo ni consuma un pago real más adelante.
    # Un pedido ya conciliado responde 409 y se queda como está, que es correcto.
    if sendhook.is_configured():
        try:
            sendhook.cancelar_pedido(str(payment_id))
        except Exception as exc:
            logger.warning("[payments.reject] cancelar_pedido falló payment_id=%s: %s", payment_id, exc)

    try:
        result = (
            supabase.table("payments")
            .update({"status": "failed"})
            .eq("id", payment_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.reject] update FAILED id=%s [%s] %s", payment_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not result.data:
        raise HTTPException(status_code=500, detail="No se pudo actualizar el pago.")

    rejected = result.data[0]
    invalidate_profile_cache(payment["user_id"])
    logger.info("[payments.reject] OK payment_id=%s", payment_id)

    # Se lee AHORA, antes de que _cleanup_failed_registration borre el usuario
    # de Auth: después de eso el email ya no se puede recuperar.
    user_email = _get_user_email(supabase, payment["user_id"])
    try:
        profile_resp = supabase.table("profiles").select("name").eq("id", payment["user_id"]).maybe_single().execute()
        user_name = (profile_resp.data or {}).get("name") or "miembro"
    except Exception:
        user_name = "miembro"
    account_deleted = False

    try:
        other_payments = (
            supabase.table("payments")
            .select("id")
            .eq("user_id", payment["user_id"])
            .neq("id", payment_id)
            .limit(1)
            .execute()
        )
        if not other_payments.data:
            user_id = payment["user_id"]
            logger.info("[payments.reject] Único pago del usuario (era el de registro) — eliminando cuenta user_id=%s", user_id)
            try:
                supabase.table("payments").update({"user_id": None}).eq("id", payment_id).execute()
            except Exception as exc:
                # Si payments.user_id no admite NULL, no queda otra que borrar
                # la fila — mejor eso que dejar el pago apuntando a una cuenta
                # que ya no existe.
                logger.warning("[payments.reject] No se pudo desvincular el pago (¿user_id NOT NULL?), se borra la fila id=%s [%s] %s", payment_id, type(exc).__name__, supabase_error(exc))
                supabase.table("payments").delete().eq("id", payment_id).execute()
            _cleanup_failed_registration(supabase, user_id)
            account_deleted = True
    except Exception as exc:
        logger.warning("[payments.reject] No se pudo verificar/eliminar la cuenta tras el rechazo user_id=%s [%s] %s", payment["user_id"], type(exc).__name__, supabase_error(exc))

    # Fire-and-forget: avisar del rechazo. El texto cambia según si la cuenta
    # sobrevivió (renovación) o se eliminó (pago de registro).
    try:
        from app.services import email as email_service
        if user_email:
            email_service.send_payment_rejected(user_email, user_name, account_deleted)
        else:
            logger.warning("[payments.reject] sin email para user_id=%s, no se notifica el rechazo", payment["user_id"])
    except Exception as exc:
        logger.warning("[payments.reject] rejection email failed: %s", exc)

    return rejected


def renew_subscription(
    user_id: str, plan: str, amount: float,
    payment_method_id: str, reference_number: str, phone: str, receipt_path: str,
    currency_id: str, amount_local: float, exchange_rate: float,
    origin_bank: Optional[str] = None, payer_id_number: Optional[str] = None,
    payer_phone: Optional[str] = None, payment_date: Optional[str] = None,
) -> dict:
    """
    Registra un pago de renovación de suscripción para un usuario ya existente.
    El pago queda en estado 'pending' (y opcionalmente intenta la verificación automática).
    """
    logger.info("[payments.renew] start - user_id=%s plan=%s", user_id, plan)
    supabase = get_supabase()

    # 0. Validar que el plan y el método de pago existan y estén activos
    plans_service.validate_active_plan(supabase, plan)
    try:
        method_resp = (
            supabase.table("payment_methods")
            .select("id, name, is_active, auto_verify")
            .eq("id", payment_method_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payments.renew] step 0 FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    method = method_resp.data
    if not method or not method.get("is_active"):
        raise HTTPException(status_code=400, detail="El método de pago seleccionado no está disponible.")

    # 1. Insertar el pago en estado pendiente de revisión (con fallback defensivo por si faltan columnas en la DB)
    insert_data = {
        "user_id": user_id,
        "plan": plan,
        "amount": amount,
        "status": "pending",
        "payment_method_id": payment_method_id,
        "payment_method": method["name"],
        "reference_number": reference_number,
        "receipt_url": receipt_path,
        "phone": phone,
        "currency_id": currency_id,
        "amount_local": amount_local,
        "exchange_rate": exchange_rate,
        "origin_bank": origin_bank,
        "payer_id_number": payer_id_number,
        "payer_phone": payer_phone,
        "payment_date": payment_date,
    }
    try:
        payment_resp = supabase.table("payments").insert(insert_data).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        if "payer_phone" in msg or "payment_date" in msg:
            logger.warning("[payments.renew] Faltan columnas en DB. Reintentando sin payer_phone/payment_date. Error: %s", msg)
            insert_data.pop("payer_phone", None)
            insert_data.pop("payment_date", None)
            try:
                payment_resp = supabase.table("payments").insert(insert_data).execute()
            except Exception as retry_exc:
                msg_retry = supabase_error(retry_exc)
                logger.error("[payments.renew] Reintento de renovación falló: %s", msg_retry)
                raise HTTPException(status_code=500, detail=f"Error al registrar el pago de renovación: {msg_retry}")
        else:
            logger.error("[payments.renew] step 1 FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al registrar el pago de renovación: {msg}")

    payment = payment_resp.data[0]
    logger.info("[payments.renew] OK - user_id=%s payment_id=%s", user_id, payment["id"])

    # Intentar la verificación automática del pago
    is_auto_verify = method.get("auto_verify", False)
    sendhook_bank = _get_destination_bank_slug(supabase, payment_method_id) if is_auto_verify else None

    approved_payment = _verify_payment_automatically(
        payment["id"], is_auto_verify, reference_number, phone,
        amount_local, sendhook_bank, payer_phone,
        # Igual que en el registro: para Binance el nombre es el único dato
        # de desambiguación, y acá hay que leerlo del perfil.
        payer_name=_get_profile_name(supabase, user_id),
    )

    if approved_payment:
        # Obtener el perfil actualizado para devolverlo al cliente y actualizar su estado inmediatamente
        try:
            profile_resp = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
            profile = profile_resp.data
            expires_at = _get_subscription_expires_at(supabase, user_id)
            user_data = {
                "id": user_id,
                "name": profile.get("name"),
                "role": profile.get("role"),
                "avatar": profile.get("avatar"),
                "bio": profile.get("bio"),
                "subscription_status": profile.get("subscription_status"),
                "subscription_expires_at": expires_at,
                "gender": profile.get("gender"),
                "city": profile.get("city"),
                "phone": profile.get("phone"),
                "birthdate": profile.get("birthdate"),
            }
        except Exception as profile_exc:
            logger.warning("[payments.renew] Failed to fetch updated profile for auto-approved: %s", profile_exc)
            user_data = None

        return {
            "payment": approved_payment,
            "user": user_data,
            "message": "¡Pago de renovación verificado automáticamente! Tu membresía ha sido reactivada.",
        }

    return {
        "payment": payment,
        "message": "Comprobante de renovación recibido. Tu pago está en revisión, te notificaremos cuando sea aprobado.",
    }


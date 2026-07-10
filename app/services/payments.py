import base64
import logging
import mimetypes
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.core.config import get_settings
from app.core.deps import invalidate_profile_cache
from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)

_PLAN_DAYS = {"1m": 30, "3m": 90, "6m": 180, "1y": 365}
_RECEIPT_BUCKET = "receipts"
_SAFE_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]")


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

    if not result.data:
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


def _normalize_phone_for_verification(phone: str) -> str:
    nums = "".join(c for c in phone if c.isdigit())
    if nums.startswith("58") and len(nums) == 12:
        return "0" + nums[2:]
    if nums.startswith("0") and len(nums) == 11:
        return nums
    if len(nums) == 10 and not nums.startswith("0"):
        return "0" + nums
    return nums


def _verify_payment_automatically(
    payment_id: str,
    method_auto_verify: bool,
    reference_number: str,
    phone: str,
    amount_local: float,
    receipt_path: str,
    banco_origen: Optional[str],
    cedula_pagador: Optional[str],
    telefono_pagador: Optional[str] = None,
    payment_date: Optional[str] = None,
) -> Optional[dict]:
    """
    Llama a la API externa de verificación de Pago Móvil.
    Si la respuesta es exitosa (status=success, pago=true), aprueba el pago
    inmediatamente y devuelve el registro aprobado.
    Si falla por cualquier motivo, devuelve None y el pago queda en 'pending'.
    """
    settings = get_settings()
    url = settings.payment_verification_url

    if not url:
        logger.info("[verify_auto] Sin URL configurada, omitiendo verificación.")
        return None

    if not method_auto_verify:
        logger.info("[verify_auto] Método sin auto_verify=True, omitiendo.")
        return None

    if not banco_origen or not cedula_pagador:
        logger.info("[verify_auto] Faltan banco_origen o cedula_pagador, omitiendo.")
        return None

    logger.info("[verify_auto] Iniciando para payment_id=%s referencia=%s", payment_id, reference_number)

    try:
        # 1. Descargar comprobante desde Supabase Storage
        supabase = get_supabase()
        file_bytes = supabase.storage.from_(_RECEIPT_BUCKET).download(receipt_path)

        # 2. Codificar en Base64 como Data URI
        mime_type, _ = mimetypes.guess_type(receipt_path)
        if not mime_type:
            mime_type = "image/jpeg"
        foto_comprobante = f"data:{mime_type};base64,{base64.b64encode(file_bytes).decode('utf-8')}"

        # 3. Normalizar teléfono al formato venezolano (04XXXXXXXXX)
        target_phone = telefono_pagador if telefono_pagador else phone
        telefono_pagador_normalized = _normalize_phone_for_verification(target_phone)

        # 4. Armar payload exacto que pide la API
        payload = {
            "metodo_pago": "pagomovil",
            "numero_referencia": reference_number,
            "banco_origen": banco_origen,
            "telefono_pagador": telefono_pagador_normalized,
            "cedula_pagador": cedula_pagador,
            "monto": float(amount_local),
            "fecha": payment_date if payment_date else datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "foto_comprobante": foto_comprobante,
        }

        # 5. Modo simulación para desarrollo (URL de ejemplo del proveedor)
        if "api.tu-marca.com" in url:
            logger.info("[verify_auto] [MOCK] URL de prueba detectada — simulando aprobación exitosa.")
            return approve_payment(payment_id)

        # 6. Llamar a la API real del proveedor
        import httpx
        logger.info("[verify_auto] Enviando solicitud a %s", url)
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(url, json=payload)
            logger.info("[verify_auto] Respuesta status=%d", resp.status_code)

            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success" and data.get("pago") is True:
                    logger.info("[verify_auto] Pago verificado por la API. Aprobando payment_id=%s", payment_id)
                    return approve_payment(payment_id)
                else:
                    logger.info("[verify_auto] API no verificó el pago. Respuesta: %s", data)
            else:
                logger.warning("[verify_auto] API respondió %d: %s", resp.status_code, resp.text)

    except Exception as exc:
        logger.exception("[verify_auto] Error en verificación automática: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Registro con pago
# ---------------------------------------------------------------------------


def register_with_payment(
    name: str, email: str, password: str, plan: str, amount: float,
    payment_method_id: str, reference_number: str, phone: str, receipt_path: str,
    currency_id: str, amount_local: float, exchange_rate: float,
    banco_origen: Optional[str] = None, cedula_pagador: Optional[str] = None,
    telefono_pagador: Optional[str] = None, payment_date: Optional[str] = None,
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

    # 0. Validar que el método de pago exista y esté activo
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
        "banco_origen": banco_origen,
        "cedula_pagador": cedula_pagador,
        "telefono_pagador": telefono_pagador,
        "payment_date": payment_date,
    }
    try:
        payment_resp = supabase.table("payments").insert(insert_data).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        if "telefono_pagador" in msg or "payment_date" in msg:
            logger.warning("[payments.register] Faltan columnas en DB. Reintentando sin telefono_pagador/payment_date. Error: %s", msg)
            insert_data.pop("telefono_pagador", None)
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
    approved_payment = _verify_payment_automatically(
        payment["id"], method.get("auto_verify", False), reference_number, phone,
        amount_local, receipt_path, banco_origen, cedula_pagador,
        telefono_pagador, payment_date
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
        if user_id not in email_cache:
            email_cache[user_id] = _get_user_email(supabase, user_id)
        row["user_email"] = email_cache[user_id]

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

def _compute_expires_at(plan: str, from_dt: datetime) -> Optional[str]:
    if plan == "indefinido":
        return None
    days = _PLAN_DAYS.get(plan)
    if days is None:
        raise HTTPException(status_code=400, detail=f"Plan desconocido: {plan}")
    return (from_dt + timedelta(days=days)).isoformat()


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
    expires_at = _compute_expires_at(payment["plan"], now)

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

    Returns:
        El registro de pago actualizado.
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

    invalidate_profile_cache(payment["user_id"])
    logger.info("[payments.reject] OK payment_id=%s", payment_id)
    return result.data[0]


def renew_subscription(
    user_id: str, plan: str, amount: float,
    payment_method_id: str, reference_number: str, phone: str, receipt_path: str,
    currency_id: str, amount_local: float, exchange_rate: float,
    banco_origen: Optional[str] = None, cedula_pagador: Optional[str] = None,
    telefono_pagador: Optional[str] = None, payment_date: Optional[str] = None,
) -> dict:
    """
    Registra un pago de renovación de suscripción para un usuario ya existente.
    El pago queda en estado 'pending' (y opcionalmente intenta la verificación automática).
    """
    logger.info("[payments.renew] start - user_id=%s plan=%s", user_id, plan)
    supabase = get_supabase()

    # 0. Validar que el método de pago exista y esté activo
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
        "banco_origen": banco_origen,
        "cedula_pagador": cedula_pagador,
        "telefono_pagador": telefono_pagador,
        "payment_date": payment_date,
    }
    try:
        payment_resp = supabase.table("payments").insert(insert_data).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        if "telefono_pagador" in msg or "payment_date" in msg:
            logger.warning("[payments.renew] Faltan columnas en DB. Reintentando sin telefono_pagador/payment_date. Error: %s", msg)
            insert_data.pop("telefono_pagador", None)
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
    approved_payment = _verify_payment_automatically(
        payment["id"], method.get("auto_verify", False), reference_number, phone,
        amount_local, receipt_path, banco_origen, cedula_pagador,
        telefono_pagador, payment_date
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


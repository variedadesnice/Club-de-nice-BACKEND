import logging
from datetime import datetime, timezone

from fastapi import HTTPException

from app.core.exceptions import supabase_error
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------

def _fetch_fields_with_values(supabase, method_ids: list[str]) -> dict[str, list[dict]]:
    """Devuelve {payment_method_id: [campos con su value]}, ordenados por sort_order.

    Los métodos sin campos quedan con lista vacía. Los campos sin valor
    configurado quedan con value=None (nunca se omiten).
    """
    grouped: dict[str, list[dict]] = {mid: [] for mid in method_ids}
    if not method_ids:
        return grouped

    try:
        fields_resp = (
            supabase.table("payment_method_fields")
            .select("*")
            .in_("payment_method_id", method_ids)
            .order("sort_order")
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods._fetch_fields] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener los campos de los métodos de pago: {msg}")

    fields = fields_resp.data or []
    field_ids = [f["id"] for f in fields]

    values_by_field: dict[str, "str | None"] = {}
    if field_ids:
        try:
            values_resp = (
                supabase.table("payment_method_values")
                .select("payment_method_field_id, value")
                .in_("payment_method_field_id", field_ids)
                .execute()
            )
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[payment_methods._fetch_fields] values FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al obtener los valores configurados: {msg}")

        for v in (values_resp.data or []):
            values_by_field[v["payment_method_field_id"]] = v["value"]

    for f in fields:
        grouped.setdefault(f["payment_method_id"], []).append({
            "id": f["id"],
            "field_key": f["field_key"],
            "field_label": f["field_label"],
            "field_type": f["field_type"],
            "is_required": f["is_required"],
            "sort_order": f["sort_order"],
            "value": values_by_field.get(f["id"]),
        })

    return grouped


def _serialize_field(f: dict, include_admin: bool) -> dict:
    item = {
        "field_key": f["field_key"],
        "field_label": f["field_label"],
        "field_type": f["field_type"],
        "value": f.get("value"),
    }
    if include_admin:
        item["id"] = f["id"]
        item["is_required"] = f["is_required"]
        item["sort_order"] = f["sort_order"]
    return item


def _serialize_method(method: dict, fields: list[dict], include_admin: bool) -> dict:
    return {
        "id": method["id"],
        "name": method["name"],
        "description": method.get("description"),
        "is_active": method["is_active"],
        "sort_order": method.get("sort_order"),
        "fields": [_serialize_field(f, include_admin) for f in fields],
    }


def _get_method_or_404(supabase, method_id: str) -> dict:
    try:
        resp = supabase.table("payment_methods").select("*").eq("id", method_id).maybe_single().execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods._get_method_or_404] FAILED id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not resp.data:
        raise HTTPException(status_code=404, detail="Método de pago no encontrado.")
    return resp.data


def _get_field_or_404(supabase, method_id: str, field_id: str) -> dict:
    try:
        resp = (
            supabase.table("payment_method_fields")
            .select("*")
            .eq("id", field_id)
            .eq("payment_method_id", method_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods._get_field_or_404] FAILED id=%s [%s] %s", field_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not resp.data:
        raise HTTPException(status_code=404, detail="El campo no existe para este método de pago.")
    return resp.data


# ---------------------------------------------------------------------------
# Públicos
# ---------------------------------------------------------------------------

def get_active_payment_methods() -> list:
    """
    Returns:
        Métodos de pago activos con sus campos y valores, ordenados por sort_order.
    Raises:
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payment_methods.get_active] fetching active methods")
    supabase = get_supabase()

    try:
        resp = (
            supabase.table("payment_methods")
            .select("*")
            .eq("is_active", True)
            .order("sort_order")
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.get_active] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener métodos de pago: {msg}")

    methods = resp.data or []
    fields_by_method = _fetch_fields_with_values(supabase, [m["id"] for m in methods])

    logger.info("[payment_methods.get_active] returned %d items", len(methods))
    return [_serialize_method(m, fields_by_method.get(m["id"], []), include_admin=False) for m in methods]


def get_active_payment_method(method_id: str) -> dict:
    """
    Returns:
        Método de pago activo con sus campos y valores.
    Raises:
        HTTPException 404 — no existe o no está activo
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payment_methods.get_active_one] method_id=%s", method_id)
    supabase = get_supabase()

    try:
        resp = (
            supabase.table("payment_methods")
            .select("*")
            .eq("id", method_id)
            .eq("is_active", True)
            .maybe_single()
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.get_active_one] FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener el método de pago: {msg}")

    method = resp.data
    if not method:
        raise HTTPException(status_code=404, detail="Método de pago no encontrado.")

    fields_by_method = _fetch_fields_with_values(supabase, [method_id])
    return _serialize_method(method, fields_by_method.get(method_id, []), include_admin=False)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def admin_get_payment_methods() -> list:
    """
    Returns:
        Todos los métodos de pago (activos e inactivos), con campos y valores.
    Raises:
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payment_methods.admin_get_all] fetching all methods")
    supabase = get_supabase()

    try:
        resp = supabase.table("payment_methods").select("*").order("sort_order").execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_get_all] FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al obtener métodos de pago: {msg}")

    methods = resp.data or []
    fields_by_method = _fetch_fields_with_values(supabase, [m["id"] for m in methods])

    logger.info("[payment_methods.admin_get_all] returned %d items", len(methods))
    return [_serialize_method(m, fields_by_method.get(m["id"], []), include_admin=True) for m in methods]


def admin_create_payment_method(data: dict) -> dict:
    """
    Crea un método de pago junto con sus campos.

    Si falla la inserción de los campos, revierte (elimina) el método recién
    creado — no hay soporte de transacciones en supabase-py.

    Returns:
        Método de pago creado, con sus campos (value=None).
    Raises:
        HTTPException 409 — ya existe un método con ese nombre, o field_key duplicado
        HTTPException 500 — fallo de base de datos
    """
    name = data["name"].strip()
    fields = data.get("fields") or []
    logger.info("[payment_methods.admin_create] name=%s fields=%d", name, len(fields))
    supabase = get_supabase()

    try:
        existing = supabase.table("payment_methods").select("id").eq("name", name).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_create] duplicate check FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if existing.data:
        raise HTTPException(status_code=409, detail=f"Ya existe un método de pago llamado '{name}'.")

    field_keys = [f["field_key"] for f in fields]
    if len(field_keys) != len(set(field_keys)):
        raise HTTPException(status_code=400, detail="Las claves de los campos (field_key) deben ser únicas.")

    try:
        method_resp = (
            supabase.table("payment_methods")
            .insert({
                "name": name,
                "description": data.get("description"),
                "is_active": data.get("is_active", True),
                "sort_order": data.get("sort_order", 0),
            })
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_create] insert method FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear el método de pago: {msg}")

    method = method_resp.data[0]
    method_id = method["id"]
    logger.info("[payment_methods.admin_create] method created id=%s", method_id)

    if fields:
        rows = [
            {
                "payment_method_id": method_id,
                "field_key": f["field_key"].strip(),
                "field_label": f["field_label"],
                "field_type": f["field_type"],
                "is_required": f.get("is_required", True),
                "sort_order": f["sort_order"] if f.get("sort_order") is not None else idx,
            }
            for idx, f in enumerate(fields)
        ]
        try:
            supabase.table("payment_method_fields").insert(rows).execute()
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[payment_methods.admin_create] insert fields FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
            try:
                supabase.table("payment_methods").delete().eq("id", method_id).execute()
            except Exception as rollback_exc:
                logger.warning("[payment_methods.admin_create] rollback FAILED method_id=%s [%s] %s", method_id, type(rollback_exc).__name__, supabase_error(rollback_exc))
            raise HTTPException(status_code=500, detail=f"Error al crear los campos del método de pago: {msg}")

    fields_by_method = _fetch_fields_with_values(supabase, [method_id])
    logger.info("[payment_methods.admin_create] OK id=%s", method_id)
    return _serialize_method(method, fields_by_method.get(method_id, []), include_admin=True)


def admin_update_payment_method(method_id: str, data: dict) -> dict:
    """
    Edita nombre, descripción, estado o sort_order de un método de pago.

    Returns:
        Método de pago actualizado, con campos y valores.
    Raises:
        HTTPException 400 — no se enviaron campos para actualizar
        HTTPException 404 — método no encontrado
        HTTPException 409 — ya existe otro método con el nuevo nombre
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payment_methods.admin_update] method_id=%s", method_id)
    supabase = get_supabase()
    _get_method_or_404(supabase, method_id)

    if not data:
        raise HTTPException(status_code=400, detail="No se enviaron campos para actualizar.")

    if "name" in data:
        name = data["name"].strip()
        try:
            existing = (
                supabase.table("payment_methods")
                .select("id")
                .eq("name", name)
                .neq("id", method_id)
                .execute()
            )
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[payment_methods.admin_update] duplicate check FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=msg)

        if existing.data:
            raise HTTPException(status_code=409, detail=f"Ya existe un método de pago llamado '{name}'.")
        data["name"] = name

    try:
        resp = supabase.table("payment_methods").update(data).eq("id", method_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_update] FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al actualizar el método de pago: {msg}")

    method = resp.data[0]
    fields_by_method = _fetch_fields_with_values(supabase, [method_id])
    logger.info("[payment_methods.admin_update] OK method_id=%s", method_id)
    return _serialize_method(method, fields_by_method.get(method_id, []), include_admin=True)


def admin_toggle_payment_method(method_id: str) -> dict:
    """
    Activa/desactiva un método de pago (invierte is_active).

    Returns:
        Método de pago actualizado, con campos y valores.
    Raises:
        HTTPException 404 — método no encontrado
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payment_methods.admin_toggle] method_id=%s", method_id)
    supabase = get_supabase()
    method = _get_method_or_404(supabase, method_id)
    new_status = not method["is_active"]

    try:
        resp = (
            supabase.table("payment_methods")
            .update({"is_active": new_status})
            .eq("id", method_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_toggle] FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al cambiar el estado del método de pago: {msg}")

    method = resp.data[0]
    fields_by_method = _fetch_fields_with_values(supabase, [method_id])
    logger.info("[payment_methods.admin_toggle] OK method_id=%s is_active=%s", method_id, new_status)
    return _serialize_method(method, fields_by_method.get(method_id, []), include_admin=True)


def admin_delete_payment_method(method_id: str) -> dict:
    """
    Elimina un método de pago junto con sus campos y valores, siempre que no
    tenga pagos asociados.

    Returns:
        {"deleted": True}
    Raises:
        HTTPException 404 — método no encontrado
        HTTPException 409 — el método tiene pagos asociados
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payment_methods.admin_delete] method_id=%s", method_id)
    supabase = get_supabase()
    _get_method_or_404(supabase, method_id)

    try:
        in_use = (
            supabase.table("payments")
            .select("id")
            .eq("payment_method_id", method_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_delete] usage check FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if in_use.data:
        raise HTTPException(status_code=409, detail="No se puede eliminar este método de pago porque tiene pagos asociados.")

    try:
        field_ids_resp = (
            supabase.table("payment_method_fields")
            .select("id")
            .eq("payment_method_id", method_id)
            .execute()
        )
        field_ids = [f["id"] for f in (field_ids_resp.data or [])]
        if field_ids:
            supabase.table("payment_method_values").delete().in_("payment_method_field_id", field_ids).execute()
        supabase.table("payment_method_fields").delete().eq("payment_method_id", method_id).execute()
        supabase.table("payment_methods").delete().eq("id", method_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_delete] FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar el método de pago: {msg}")

    logger.info("[payment_methods.admin_delete] OK method_id=%s", method_id)
    return {"deleted": True}


def admin_set_payment_method_values(method_id: str, values: list[dict]) -> dict:
    """
    Configura (upsert) los valores de los campos de un método de pago.

    Returns:
        Método de pago actualizado, con campos y valores.
    Raises:
        HTTPException 400 — algún field_key no existe para este método
        HTTPException 404 — método no encontrado
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payment_methods.admin_set_values] method_id=%s count=%d", method_id, len(values))
    supabase = get_supabase()
    method = _get_method_or_404(supabase, method_id)

    try:
        fields_resp = (
            supabase.table("payment_method_fields")
            .select("id, field_key")
            .eq("payment_method_id", method_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_set_values] fields lookup FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    field_by_key = {f["field_key"]: f["id"] for f in (fields_resp.data or [])}

    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for item in values:
        field_key = item["field_key"]
        field_id = field_by_key.get(field_key)
        if not field_id:
            raise HTTPException(status_code=400, detail=f"El campo '{field_key}' no existe para este método de pago.")
        rows.append({
            "payment_method_field_id": field_id,
            "value": item.get("value"),
            "updated_at": now,
        })

    if rows:
        try:
            supabase.table("payment_method_values").upsert(rows, on_conflict="payment_method_field_id").execute()
        except Exception as exc:
            msg = supabase_error(exc)
            logger.error("[payment_methods.admin_set_values] upsert FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error al guardar los valores: {msg}")

    fields_by_method = _fetch_fields_with_values(supabase, [method_id])
    logger.info("[payment_methods.admin_set_values] OK method_id=%s", method_id)
    return _serialize_method(method, fields_by_method.get(method_id, []), include_admin=True)


def admin_add_field(method_id: str, data: dict) -> dict:
    """
    Agrega un nuevo campo a un método de pago existente.

    Returns:
        Campo creado (value=None).
    Raises:
        HTTPException 404 — método no encontrado
        HTTPException 409 — ya existe un campo con ese field_key en este método
        HTTPException 500 — fallo de base de datos
    """
    field_key = data["field_key"].strip()
    logger.info("[payment_methods.admin_add_field] method_id=%s field_key=%s", method_id, field_key)
    supabase = get_supabase()
    _get_method_or_404(supabase, method_id)

    try:
        existing = (
            supabase.table("payment_method_fields")
            .select("id")
            .eq("payment_method_id", method_id)
            .eq("field_key", field_key)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_add_field] duplicate check FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if existing.data:
        raise HTTPException(status_code=409, detail=f"Ya existe un campo con clave '{field_key}' para este método de pago.")

    sort_order = data.get("sort_order")
    if sort_order is None:
        try:
            count_resp = (
                supabase.table("payment_method_fields")
                .select("id", count="exact")
                .eq("payment_method_id", method_id)
                .execute()
            )
            sort_order = count_resp.count or 0
        except Exception as exc:
            logger.warning("[payment_methods.admin_add_field] sort_order count FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, supabase_error(exc))
            sort_order = 0

    try:
        resp = (
            supabase.table("payment_method_fields")
            .insert({
                "payment_method_id": method_id,
                "field_key": field_key,
                "field_label": data["field_label"],
                "field_type": data["field_type"],
                "is_required": data.get("is_required", True),
                "sort_order": sort_order,
            })
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_add_field] insert FAILED method_id=%s [%s] %s", method_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear el campo: {msg}")

    field = resp.data[0]
    field["value"] = None
    logger.info("[payment_methods.admin_add_field] OK field_id=%s", field["id"])
    return _serialize_field(field, include_admin=True)


def admin_update_field(method_id: str, field_id: str, data: dict) -> dict:
    """
    Edita un campo existente (label, tipo, requerido, orden).

    Returns:
        Campo actualizado, con su value actual.
    Raises:
        HTTPException 400 — no se enviaron campos para actualizar
        HTTPException 404 — método o campo no encontrado
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payment_methods.admin_update_field] method_id=%s field_id=%s", method_id, field_id)
    supabase = get_supabase()
    _get_method_or_404(supabase, method_id)
    _get_field_or_404(supabase, method_id, field_id)

    if not data:
        raise HTTPException(status_code=400, detail="No se enviaron campos para actualizar.")

    try:
        resp = (
            supabase.table("payment_method_fields")
            .update(data)
            .eq("id", field_id)
            .execute()
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_update_field] FAILED field_id=%s [%s] %s", field_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al actualizar el campo: {msg}")

    field = resp.data[0]

    try:
        value_resp = (
            supabase.table("payment_method_values")
            .select("value")
            .eq("payment_method_field_id", field_id)
            .maybe_single()
            .execute()
        )
        field["value"] = value_resp.data["value"] if value_resp.data else None
    except Exception as exc:
        logger.warning("[payment_methods.admin_update_field] value lookup FAILED field_id=%s [%s] %s", field_id, type(exc).__name__, supabase_error(exc))
        field["value"] = None

    logger.info("[payment_methods.admin_update_field] OK field_id=%s", field_id)
    return _serialize_field(field, include_admin=True)


def admin_delete_field(method_id: str, field_id: str) -> dict:
    """
    Elimina un campo y su valor configurado en cascada.

    Returns:
        {"deleted": True}
    Raises:
        HTTPException 404 — método o campo no encontrado
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[payment_methods.admin_delete_field] method_id=%s field_id=%s", method_id, field_id)
    supabase = get_supabase()
    _get_method_or_404(supabase, method_id)
    _get_field_or_404(supabase, method_id, field_id)

    try:
        supabase.table("payment_method_values").delete().eq("payment_method_field_id", field_id).execute()
        supabase.table("payment_method_fields").delete().eq("id", field_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[payment_methods.admin_delete_field] FAILED field_id=%s [%s] %s", field_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al eliminar el campo: {msg}")

    logger.info("[payment_methods.admin_delete_field] OK field_id=%s", field_id)
    return {"deleted": True}

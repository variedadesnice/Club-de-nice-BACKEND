from fastapi import APIRouter, Depends

from app.core.deps import get_current_admin
from app.schemas.payment_methods import (
    AddPaymentMethodFieldRequest,
    CreatePaymentMethodRequest,
    SetPaymentMethodValuesRequest,
    UpdatePaymentMethodFieldRequest,
    UpdatePaymentMethodRequest,
)
from app.services import payment_methods as payment_methods_service

router = APIRouter()


@router.get("/")
def admin_get_payment_methods(current_user: dict = Depends(get_current_admin)):
    """Admin — lista todos los métodos de pago (activos e inactivos) con campos y valores."""
    return payment_methods_service.admin_get_payment_methods()


@router.post("/", status_code=201)
def admin_create_payment_method(body: CreatePaymentMethodRequest, current_user: dict = Depends(get_current_admin)):
    """Admin — crea un método de pago junto con sus campos."""
    return payment_methods_service.admin_create_payment_method(body.model_dump())


@router.patch("/{method_id}")
def admin_update_payment_method(method_id: str, body: UpdatePaymentMethodRequest, current_user: dict = Depends(get_current_admin)):
    """Admin — edita nombre, descripción, estado o sort_order de un método de pago."""
    return payment_methods_service.admin_update_payment_method(method_id, body.model_dump(exclude_none=True))


@router.patch("/{method_id}/toggle")
def admin_toggle_payment_method(method_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin — activa/desactiva un método de pago."""
    return payment_methods_service.admin_toggle_payment_method(method_id)


@router.delete("/{method_id}")
def admin_delete_payment_method(method_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin — elimina un método de pago (409 si tiene pagos asociados)."""
    return payment_methods_service.admin_delete_payment_method(method_id)


@router.put("/{method_id}/values")
def admin_set_payment_method_values(method_id: str, body: SetPaymentMethodValuesRequest, current_user: dict = Depends(get_current_admin)):
    """Admin — configura (upsert) los valores de los campos de un método de pago."""
    return payment_methods_service.admin_set_payment_method_values(method_id, [v.model_dump() for v in body.values])


@router.post("/{method_id}/fields", status_code=201)
def admin_add_payment_method_field(method_id: str, body: AddPaymentMethodFieldRequest, current_user: dict = Depends(get_current_admin)):
    """Admin — agrega un campo nuevo a un método de pago existente."""
    return payment_methods_service.admin_add_field(method_id, body.model_dump())


@router.patch("/{method_id}/fields/{field_id}")
def admin_update_payment_method_field(method_id: str, field_id: str, body: UpdatePaymentMethodFieldRequest, current_user: dict = Depends(get_current_admin)):
    """Admin — edita un campo (label, tipo, requerido, orden)."""
    return payment_methods_service.admin_update_field(method_id, field_id, body.model_dump(exclude_none=True))


@router.delete("/{method_id}/fields/{field_id}")
def admin_delete_payment_method_field(method_id: str, field_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin — elimina un campo y su valor configurado en cascada."""
    return payment_methods_service.admin_delete_field(method_id, field_id)

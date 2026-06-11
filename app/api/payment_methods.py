from fastapi import APIRouter

from app.services import payment_methods as payment_methods_service

router = APIRouter()


@router.get("/")
def get_payment_methods():
    """Público — lista los métodos de pago activos con sus campos y valores configurados."""
    return payment_methods_service.get_active_payment_methods()


@router.get("/{method_id}")
def get_payment_method(method_id: str):
    """Público — detalle de un método de pago activo con sus campos y valores."""
    return payment_methods_service.get_active_payment_method(method_id)

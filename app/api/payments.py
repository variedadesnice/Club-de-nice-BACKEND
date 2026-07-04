from fastapi import APIRouter, Depends

from app.core.deps import get_current_admin, get_current_user
from app.core.rate_limit import rate_limiter
from app.schemas.payments import RegisterWithPaymentRequest, UploadReceiptRequest, RenewSubscriptionRequest
from app.services import payments as payments_service

router = APIRouter()


@router.post("/upload-receipt")
def upload_receipt(body: UploadReceiptRequest):
    """Público — sube el comprobante al bucket receipts antes del registro y devuelve su path."""
    return payments_service.upload_receipt(body.reference_number, body.filename, body.fileData)


@router.post("/register", status_code=201, dependencies=[Depends(rate_limiter(5, 600, "payments-register"))])
def register_with_payment(body: RegisterWithPaymentRequest):
    """Público — registra al usuario, crea su perfil inactivo y deja el pago en revisión ('pending')."""
    return payments_service.register_with_payment(
        body.name, body.email, body.password, body.plan, body.amount,
        body.payment_method_id, body.reference_number, body.phone, body.receipt_path,
        body.currency_id, body.amount_local, body.exchange_rate,
    )


@router.post("/renew", status_code=201, dependencies=[Depends(rate_limiter(5, 600, "payments-renew"))])
def renew_subscription(body: RenewSubscriptionRequest, current_user: dict = Depends(get_current_user)):
    """Autenticado — registra un pago de renovación de suscripción para el usuario actual."""
    return payments_service.renew_subscription(
        current_user["id"], body.plan, body.amount,
        body.payment_method_id, body.reference_number, body.phone, body.receipt_path,
        body.currency_id, body.amount_local, body.exchange_rate,
    )


@router.get("/")
def list_payments(current_user: dict = Depends(get_current_admin)):
    """Admin — lista todos los pagos con el nombre y email del usuario asociado."""
    return payments_service.list_payments()


@router.get("/{user_id}")
def get_user_payments(user_id: str, current_user: dict = Depends(get_current_user)):
    """Admin o el propio usuario — historial de pagos de user_id."""
    return payments_service.get_user_payments(user_id, current_user["id"])


@router.patch("/{payment_id}/approve")
def approve_payment(payment_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin aprueba el pago: status -> 'success' y calcula expires_at según el plan."""
    return payments_service.approve_payment(payment_id)


@router.patch("/{payment_id}/reject")
def reject_payment(payment_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin rechaza el pago: status -> 'failed'."""
    return payments_service.reject_payment(payment_id)


@router.get("/{payment_id}/receipt")
def get_receipt_url(payment_id: str, current_user: dict = Depends(get_current_admin)):
    """Admin obtiene una signed URL temporal (1 hora) del comprobante."""
    return payments_service.get_receipt_signed_url(payment_id)

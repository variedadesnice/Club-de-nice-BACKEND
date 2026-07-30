from typing import Optional

from pydantic import BaseModel, Field

# Código de un plan activo en la tabla `plans` (admin-configurable desde
# /api/admin/plans) — ya no es un set fijo, se valida contra la tabla en
# el service (plans_service.validate_active_plan).
PlanType = str


class UploadReceiptRequest(BaseModel):
    reference_number: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1)
    fileData: str = Field(..., description="data URI base64: data:<mime>;base64,<...>")


class RegisterWithPaymentRequest(BaseModel):
    name: str = Field(..., min_length=1)
    email: str
    password: str = Field(..., min_length=6, description="Mínimo 6 caracteres")
    plan: PlanType = Field(..., min_length=1)
    amount: float = Field(..., gt=0, description="Monto en USD (base para reportes)")
    payment_method_id: str = Field(..., min_length=1)
    reference_number: str = Field(..., min_length=1)
    phone: str = Field(..., min_length=1)
    receipt_path: str = Field(..., min_length=1, description="Path devuelto por /payments/upload-receipt")
    currency_id: str = Field(..., min_length=1, description="UUID de la divisa en la que pagó el usuario")
    amount_local: float = Field(..., gt=0, description="Monto real en la divisa local (ej. 400 Bs.)")
    exchange_rate: float = Field(..., gt=0, description="Tasa congelada en el momento del pago (1 USD = X local)")
    origin_bank: Optional[str] = Field(None, description="Código de 4 dígitos del banco emisor (ej. 0102)")
    payer_id_number: Optional[str] = Field(None, description="Cédula del pagador (ej. V12177212)")
    payer_phone: Optional[str] = Field(None, description="Teléfono del pagador (ej. 04246296646)")
    payment_date: Optional[str] = Field(None, description="Fecha del pago (ej. 2026-06-06)")


class RenewSubscriptionRequest(BaseModel):
    plan: PlanType = Field(..., min_length=1)
    amount: float = Field(..., gt=0, description="Monto en USD (base para reportes)")
    payment_method_id: str = Field(..., min_length=1)
    reference_number: str = Field(..., min_length=1)
    phone: str = Field(..., min_length=1)
    receipt_path: str = Field(..., min_length=1, description="Path devuelto por /payments/upload-receipt")
    currency_id: str = Field(..., min_length=1, description="UUID de la divisa en la que pagó el usuario")
    amount_local: float = Field(..., gt=0, description="Monto real en la divisa local (ej. 400 Bs.)")
    exchange_rate: float = Field(..., gt=0, description="Tasa congelada en el momento del pago (1 USD = X local)")
    origin_bank: Optional[str] = Field(None, description="Código de 4 dígitos del banco emisor (ej. 0102)")
    payer_id_number: Optional[str] = Field(None, description="Cédula del pagador (ej. V12177212)")
    payer_phone: Optional[str] = Field(None, description="Teléfono del pagador (ej. 04246296646)")
    payment_date: Optional[str] = Field(None, description="Fecha del pago (ej. 2026-06-06)")



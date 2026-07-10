from typing import Literal, Optional

from pydantic import BaseModel, Field

PlanType = Literal["1m", "3m", "6m", "1y", "indefinido"]


class UploadReceiptRequest(BaseModel):
    reference_number: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1)
    fileData: str = Field(..., description="data URI base64: data:<mime>;base64,<...>")


class RegisterWithPaymentRequest(BaseModel):
    name: str = Field(..., min_length=1)
    email: str
    password: str = Field(..., min_length=6, description="Mínimo 6 caracteres")
    plan: PlanType
    amount: float = Field(..., gt=0, description="Monto en USD (base para reportes)")
    payment_method_id: str = Field(..., min_length=1)
    reference_number: str = Field(..., min_length=1)
    phone: str = Field(..., min_length=1)
    receipt_path: str = Field(..., min_length=1, description="Path devuelto por /payments/upload-receipt")
    currency_id: str = Field(..., min_length=1, description="UUID de la divisa en la que pagó el usuario")
    amount_local: float = Field(..., gt=0, description="Monto real en la divisa local (ej. 400 Bs.)")
    exchange_rate: float = Field(..., gt=0, description="Tasa congelada en el momento del pago (1 USD = X local)")
    banco_origen: Optional[str] = Field(None, description="Código de 4 dígitos del banco emisor (ej. 0102)")
    cedula_pagador: Optional[str] = Field(None, description="Cédula del pagador (ej. V12177212)")
    telefono_pagador: Optional[str] = Field(None, description="Teléfono del pagador (ej. 04246296646)")
    payment_date: Optional[str] = Field(None, description="Fecha del pago (ej. 2026-06-06)")


class RenewSubscriptionRequest(BaseModel):
    plan: PlanType
    amount: float = Field(..., gt=0, description="Monto en USD (base para reportes)")
    payment_method_id: str = Field(..., min_length=1)
    reference_number: str = Field(..., min_length=1)
    phone: str = Field(..., min_length=1)
    receipt_path: str = Field(..., min_length=1, description="Path devuelto por /payments/upload-receipt")
    currency_id: str = Field(..., min_length=1, description="UUID de la divisa en la que pagó el usuario")
    amount_local: float = Field(..., gt=0, description="Monto real en la divisa local (ej. 400 Bs.)")
    exchange_rate: float = Field(..., gt=0, description="Tasa congelada en el momento del pago (1 USD = X local)")
    banco_origen: Optional[str] = Field(None, description="Código de 4 dígitos del banco emisor (ej. 0102)")
    cedula_pagador: Optional[str] = Field(None, description="Cédula del pagador (ej. V12177212)")
    telefono_pagador: Optional[str] = Field(None, description="Teléfono del pagador (ej. 04246296646)")
    payment_date: Optional[str] = Field(None, description="Fecha del pago (ej. 2026-06-06)")



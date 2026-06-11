from typing import Literal, Optional

from pydantic import BaseModel, Field

FieldType = Literal["text", "email", "phone", "number"]


class PaymentMethodFieldRequest(BaseModel):
    field_key: str = Field(..., min_length=1)
    field_label: str = Field(..., min_length=1)
    field_type: FieldType
    is_required: bool = True
    sort_order: Optional[int] = Field(None, ge=0)


class CreatePaymentMethodRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    is_active: bool = True
    sort_order: int = Field(0, ge=0)
    fields: list[PaymentMethodFieldRequest] = Field(default_factory=list)


class UpdatePaymentMethodRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0)


class AddPaymentMethodFieldRequest(BaseModel):
    field_key: str = Field(..., min_length=1)
    field_label: str = Field(..., min_length=1)
    field_type: FieldType
    is_required: bool = True
    sort_order: Optional[int] = Field(None, ge=0)


class UpdatePaymentMethodFieldRequest(BaseModel):
    field_label: Optional[str] = Field(None, min_length=1)
    field_type: Optional[FieldType] = None
    is_required: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0)


class PaymentMethodValueItem(BaseModel):
    field_key: str = Field(..., min_length=1)
    value: Optional[str] = None


class SetPaymentMethodValuesRequest(BaseModel):
    values: list[PaymentMethodValueItem] = Field(default_factory=list)

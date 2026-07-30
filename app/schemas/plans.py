from typing import Optional

from pydantic import BaseModel, Field


class PlanCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=30)
    name: str = Field(..., min_length=1)
    sublabel: Optional[str] = None
    duration_days: Optional[int] = Field(None, gt=0, description="None = indefinido, sin vencimiento")
    price_usd: float = Field(..., ge=0)
    is_active: bool = True
    sort_order: int = Field(0, ge=0)


class PlanUpdate(BaseModel):
    code: Optional[str] = Field(None, min_length=1, max_length=30)
    name: Optional[str] = Field(None, min_length=1)
    sublabel: Optional[str] = None
    duration_days: Optional[int] = Field(None, gt=0)
    price_usd: Optional[float] = Field(None, ge=0)
    sort_order: Optional[int] = Field(None, ge=0)

from typing import List, Optional

from pydantic import BaseModel, Field


class PrizeOut(BaseModel):
    id: str
    label: str
    color: str
    weight: int


class PublicPrizeOut(BaseModel):
    id: str
    label: str
    color: str


class CreatePrizeRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=30)
    color: Optional[str] = None
    weight: int = Field(1, ge=1, le=1000)


class UpdatePrizeRequest(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=30)
    color: Optional[str] = None
    weight: Optional[int] = Field(None, ge=1, le=1000)


class RouletteSettingsOut(BaseModel):
    is_active: bool
    prizes: List[PrizeOut]


class UpdateSettingsRequest(BaseModel):
    is_active: bool


class RouletteStatusOut(BaseModel):
    is_active: bool
    already_spun_today: bool
    prizes: List[PublicPrizeOut]


class SpinOut(BaseModel):
    prize_id: str
    label: str
    color: str


class SpinHistoryItem(BaseModel):
    id: str
    user_id: str
    user_name: str
    prize_label: str
    spun_at: str

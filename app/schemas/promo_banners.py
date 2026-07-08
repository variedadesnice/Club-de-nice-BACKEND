from typing import Optional

from pydantic import BaseModel, Field


class PromoBannerOut(BaseModel):
    id: str
    title: str
    description: str
    image_url: str
    link_url: str
    is_active: bool
    created_at: str


class CreatePromoBannerRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=150)
    description: str = Field(..., min_length=1, max_length=500)
    image_url: str = Field(..., min_length=1)
    link_url: str = Field(..., min_length=1, max_length=500)


class UpdatePromoBannerRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=150)
    description: Optional[str] = Field(None, min_length=1, max_length=500)
    image_url: Optional[str] = None
    link_url: Optional[str] = Field(None, min_length=1, max_length=500)


class SetActiveRequest(BaseModel):
    is_active: bool

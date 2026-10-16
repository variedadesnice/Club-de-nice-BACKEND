from typing import Literal, Optional

from pydantic import BaseModel, Field

# Debe coincidir con el CHECK de content_categories.icon (sql/content_categories.sql)
# y con los dibujos que tiene el frontend para cada clave.
CategoryIcon = Literal["play", "check", "doc", "flask"]


class ContentCategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    description: Optional[str] = Field(None, max_length=120)
    icon: CategoryIcon = "play"
    sort_order: int = Field(0, ge=0)
    is_active: bool = True


class ContentCategoryUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    description: Optional[str] = Field(None, max_length=120)
    icon: Optional[CategoryIcon] = None
    sort_order: Optional[int] = Field(None, ge=0)

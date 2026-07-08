from typing import List, Optional

from pydantic import BaseModel, Field


class WinnerOut(BaseModel):
    id: str
    user_id: str
    position: int
    name: str
    avatar: Optional[str] = None
    email: Optional[str] = None


class RaffleOut(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    winner_count: int
    draw_at: Optional[str] = None
    drawn_at: Optional[str] = None
    is_active: bool
    created_at: str
    winners: List[WinnerOut]


class CreateRaffleRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = Field(None, max_length=1000)
    image_url: Optional[str] = None
    winner_count: int = Field(..., ge=1, le=20)
    draw_at: str

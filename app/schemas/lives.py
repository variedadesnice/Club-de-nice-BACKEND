from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class LiveSessionBase(BaseModel):
    title: str
    youtube_url: str

class LiveSessionCreate(LiveSessionBase):
    pass

class LiveSessionResponse(LiveSessionBase):
    id: str
    is_active: bool
    created_at: datetime

class ChatMessageBase(BaseModel):
    content: str

class ChatMessageCreate(ChatMessageBase):
    pass

class ChatMessageResponse(ChatMessageBase):
    id: str
    live_id: str
    user_id: str
    created_at: datetime
    # We add user info for the frontend
    author_name: str
    author_avatar: Optional[str] = None
    author_role: str

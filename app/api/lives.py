from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
from app.core.supabase import get_supabase
from supabase import Client
from app.api.auth import get_current_user
from app.schemas.lives import LiveSessionCreate, LiveSessionResponse, ChatMessageCreate, ChatMessageResponse
from app.services.lives import LivesService

router = APIRouter()

def get_lives_service(supabase: Client = Depends(get_supabase)) -> LivesService:
    return LivesService(supabase)

def require_admin(user: dict):
    role = user.get("role")
    if role not in ["admin", "superadmin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requieren permisos de administrador"
        )

@router.get("/current", response_model=Optional[LiveSessionResponse])
def get_current_live(
    current_user: dict = Depends(get_current_user),
    service: LivesService = Depends(get_lives_service)
):
    try:
        return service.get_current_live()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/", response_model=LiveSessionResponse)
def create_live(
    data: LiveSessionCreate,
    current_user: dict = Depends(get_current_user),
    service: LivesService = Depends(get_lives_service)
):
    require_admin(current_user)
    try:
        return service.create_live(data.title, data.youtube_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{live_id}/end", response_model=LiveSessionResponse)
def end_live(
    live_id: str,
    current_user: dict = Depends(get_current_user),
    service: LivesService = Depends(get_lives_service)
):
    require_admin(current_user)
    try:
        return service.end_live(live_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/{live_id}/chat", response_model=List[ChatMessageResponse])
def get_chat_messages(
    live_id: str,
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
    service: LivesService = Depends(get_lives_service)
):
    try:
        return service.get_chat_messages(live_id, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{live_id}/chat", response_model=ChatMessageResponse)
def send_chat_message(
    live_id: str,
    data: ChatMessageCreate,
    current_user: dict = Depends(get_current_user),
    service: LivesService = Depends(get_lives_service)
):
    try:
        return service.send_chat_message(live_id, current_user["id"], data.content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

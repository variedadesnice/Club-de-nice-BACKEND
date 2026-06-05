import base64
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lib.supabase import get_supabase

router = APIRouter()


class RegisterBody(BaseModel):
    name: str
    email: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


class AvatarBody(BaseModel):
    userId: str
    imageData: str


class ProfileBody(BaseModel):
    id: str
    name: str
    avatar: str
    bio: str


@router.post("/register", status_code=201)
def register(body: RegisterBody):
    if not body.name or not body.email or not body.password:
        raise HTTPException(status_code=400, detail="Faltan campos requeridos")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 6 caracteres")

    supabase = get_supabase()

    try:
        auth_response = supabase.auth.admin.create_user({
            "email": body.email,
            "password": body.password,
            "email_confirm": True,
        })
    except Exception as e:
        err_msg = str(e).lower()
        if "already registered" in err_msg or "already been registered" in err_msg:
            raise HTTPException(status_code=400, detail="Este email ya está registrado")
        raise HTTPException(status_code=400, detail=str(e))

    user_id = auth_response.user.id
    avatar = f"https://i.pravatar.cc/150?u={user_id}"

    try:
        supabase.table("profiles").insert({
            "id": user_id,
            "name": body.name,
            "role": "miembro",
            "avatar": avatar,
            "bio": "",
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al crear perfil: {str(e)}")

    try:
        session_response = supabase.auth.sign_in_with_password({
            "email": body.email,
            "password": body.password,
        })
        token = session_response.session.access_token
    except Exception:
        return JSONResponse(
            status_code=201,
            content={"requiresEmailConfirmation": False, "autoLogin": False},
        )

    return JSONResponse(
        status_code=201,
        content={
            "user": {
                "id": user_id,
                "name": body.name,
                "email": body.email,
                "role": "miembro",
                "avatar": avatar,
                "bio": "",
            },
            "token": token,
        },
    )


@router.post("/login")
def login(body: LoginBody):
    supabase = get_supabase()

    try:
        session_response = supabase.auth.sign_in_with_password({
            "email": body.email,
            "password": body.password,
        })
    except Exception:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    user = session_response.user
    token = session_response.session.access_token

    try:
        profile_response = supabase.table("profiles").select("*").eq("id", user.id).single().execute()
        profile = profile_response.data
    except Exception:
        profile = None

    if not profile:
        default_name = body.email.split("@")[0]
        default_avatar = f"https://i.pravatar.cc/150?u={user.id}"
        supabase.table("profiles").upsert({
            "id": user.id,
            "name": default_name,
            "role": "miembro",
            "avatar": default_avatar,
            "bio": "",
        }).execute()
        profile = {"name": default_name, "role": "miembro", "avatar": default_avatar, "bio": ""}

    return {
        "user": {
            "id": user.id,
            "name": profile.get("name"),
            "email": user.email,
            "role": profile.get("role"),
            "avatar": profile.get("avatar"),
            "bio": profile.get("bio"),
        },
        "token": token,
    }


@router.post("/avatar")
def upload_avatar(body: AvatarBody):
    match = re.match(r"^data:(.+);base64,(.+)$", body.imageData)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de imagen inválido")

    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))

    supabase = get_supabase()
    path = f"avatar-{body.userId}.jpg"

    supabase.storage.from_("Avatars").upload(
        path,
        raw_bytes,
        file_options={"content-type": mime_type, "upsert": "true"},
    )

    url = supabase.storage.from_("Avatars").get_public_url(path)
    return {"url": url}


@router.put("/profile")
def update_profile(body: ProfileBody):
    supabase = get_supabase()

    supabase.table("profiles").update({
        "name": body.name,
        "avatar": body.avatar,
        "bio": body.bio,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.id).execute()

    try:
        profile_response = supabase.table("profiles").select("role").eq("id", body.id).single().execute()
        role = profile_response.data.get("role") if profile_response.data else "miembro"
    except Exception:
        role = "miembro"

    try:
        auth_user = supabase.auth.admin.get_user_by_id(body.id)
        email = auth_user.user.email if auth_user.user else None
    except Exception:
        email = None

    return {
        "user": {
            "id": body.id,
            "name": body.name,
            "email": email,
            "role": role,
            "avatar": body.avatar,
            "bio": body.bio,
        }
    }

import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.core.exceptions import supabase_error
from app.core.supabase import create_anon_client, get_supabase

logger = logging.getLogger(__name__)


def _award(user_id: str, code: str, metadata: dict = None) -> None:
    """Fire-and-forget: otorga un logro sin bloquear ni lanzar excepciones."""
    try:
        from app.services.levels import process_achievement
        process_achievement(user_id, code, metadata)
    except Exception as exc:
        logger.warning("[auth._award] silenced error user_id=%s code=%s [%s]", user_id, code, exc)


def register(name: str, email: str, password: str, role: str = "miembro") -> dict:
    """
    Returns:
        {"auto_login": True,  "user": {...}, "token": "..."}  — cuenta creada y sesión activa
        {"auto_login": False}                                  — cuenta creada pero sin sesión
    Raises:
        HTTPException 400 — email ya registrado u otro error de Supabase Auth
        HTTPException 500 — fallo al insertar el perfil
    """
    logger.info("[auth.register] start - email=%s", email)
    supabase = get_supabase()

    # 1. Crear usuario en Supabase Auth
    logger.info("[auth.register] step 1/3 - creating auth user")
    try:
        auth_resp = supabase.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
        })
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[auth.register] step 1/3 FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        msg_lower = msg.lower()
        if "already registered" in msg_lower or "already been registered" in msg_lower:
            raise HTTPException(status_code=400, detail="Este email ya está registrado. Intenta iniciar sesión.")
        raise HTTPException(status_code=400, detail=f"Error al crear usuario en Supabase: {msg}")

    user_id = auth_resp.user.id
    avatar = f"https://i.pravatar.cc/150?u={user_id}"
    logger.info("[auth.register] step 1/3 OK - user_id=%s", user_id)

    # 2. Insertar fila en profiles
    logger.info("[auth.register] step 2/3 - inserting profile")
    try:
        supabase.table("profiles").insert({
            "id": user_id,
            "name": name,
            "role": role,
            "avatar": avatar,
            "bio": "",
        }).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[auth.register] step 2/3 FAILED [%s] %s", type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error al crear perfil: {msg}")

    logger.info("[auth.register] step 2/3 OK")

    # 3. Auto-login — uses a fresh client to avoid contaminating the admin singleton's session state
    logger.info("[auth.register] step 3/3 - attempting auto-login")
    try:
        anon_client = create_anon_client()
        session_resp = anon_client.auth.sign_in_with_password({"email": email, "password": password})
        token = session_resp.session.access_token
    except Exception as exc:
        logger.warning(
            "[auth.register] step 3/3 auto-login failed (account created OK) [%s] %s",
            type(exc).__name__, supabase_error(exc),
        )
        return {"auto_login": False}

    logger.info("[auth.register] step 3/3 OK - user_id=%s", user_id)
    return {
        "auto_login": True,
        "user": {"id": user_id, "name": name, "email": email, "role": role, "avatar": avatar, "bio": ""},
        "token": token,
    }


def login(email: str, password: str) -> dict:
    """
    Returns:
        {"user": {...}, "token": "..."}
    Raises:
        HTTPException 401 — credenciales inválidas o email sin confirmar
    """
    logger.info("[auth.login] start - email=%s", email)
    supabase = get_supabase()
    anon_client = create_anon_client()

    try:
        session_resp = anon_client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:
        msg = supabase_error(exc)
        logger.warning("[auth.login] FAILED for %s [%s] %s", email, type(exc).__name__, msg)
        if "email not confirmed" in msg.lower():
            raise HTTPException(status_code=401, detail="Debes confirmar tu email antes de iniciar sesión.")
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos.")

    user = session_resp.user
    token = session_resp.session.access_token
    logger.info("[auth.login] OK - user_id=%s", user.id)

    try:
        profile_resp = supabase.table("profiles").select("*").eq("id", user.id).single().execute()
        profile = profile_resp.data
    except Exception as exc:
        logger.warning("[auth.login] profile fetch failed user_id=%s [%s]", user.id, supabase_error(exc))
        profile = None

    if not profile:
        default_name = email.split("@")[0]
        default_avatar = f"https://i.pravatar.cc/150?u={user.id}"
        logger.info("[auth.login] no profile found, upserting default for user_id=%s", user.id)
        supabase.table("profiles").upsert({
            "id": user.id, "name": default_name, "role": "miembro",
            "avatar": default_avatar, "bio": "",
        }).execute()
        profile = {"name": default_name, "role": "miembro", "avatar": default_avatar, "bio": "", "subscription_status": None}

    _award(user.id, "first_login")
    return {
        "user": {
            "id": user.id,
            "name": profile.get("name"),
            "email": user.email,
            "role": profile.get("role"),
            "avatar": profile.get("avatar"),
            "bio": profile.get("bio"),
            "subscription_status": profile.get("subscription_status"),
            "gender": profile.get("gender"),
            "city": profile.get("city"),
            "phone": profile.get("phone"),
        },
        "token": token,
    }


def get_me(user_id: str, email: str) -> dict:
    """
    Devuelve el perfil actual del usuario autenticado (incluye subscription_status),
    útil para refrescar el estado de cuenta sin tener que reloguearse.

    Returns:
        {"user": {...}}
    Raises:
        HTTPException 404 — perfil no encontrado
        HTTPException 500 — fallo de base de datos
    """
    logger.info("[auth.get_me] id=%s", user_id)
    supabase = get_supabase()

    try:
        profile_resp = supabase.table("profiles").select("*").eq("id", user_id).maybe_single().execute()
        profile = profile_resp.data
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[auth.get_me] FAILED id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    if not profile:
        raise HTTPException(status_code=404, detail="Perfil no encontrado.")

    return {
        "user": {
            "id": user_id,
            "name": profile.get("name"),
            "email": email,
            "role": profile.get("role"),
            "avatar": profile.get("avatar"),
            "bio": profile.get("bio"),
            "subscription_status": profile.get("subscription_status"),
            "gender": profile.get("gender"),
            "city": profile.get("city"),
            "phone": profile.get("phone"),
        }
    }


def upload_avatar(user_id: str, image_data: str) -> dict:
    """
    Returns:
        {"url": "https://..."}
    Raises:
        HTTPException 400 — formato de imagen inválido
        HTTPException 500 — fallo al subir a Supabase Storage
    """
    import base64
    import re
    logger.info("[auth.upload_avatar] userId=%s", user_id)

    match = re.match(r"^data:(.+);base64,(.+)$", image_data)
    if not match:
        raise HTTPException(status_code=400, detail="Formato de imagen inválido")

    mime_type = match.group(1)
    raw_bytes = base64.b64decode(match.group(2))
    supabase = get_supabase()
    path = f"avatar-{user_id}.jpg"

    try:
        supabase.storage.from_("Avatars").upload(
            path, raw_bytes,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[auth.upload_avatar] upload FAILED userId=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    url = supabase.storage.from_("Avatars").get_public_url(path)
    logger.info("[auth.upload_avatar] OK userId=%s", user_id)
    _award(user_id, "avatar_uploaded")
    return {"url": url}


def update_profile(user_id: str, name: str, avatar: str, bio: str,
                   gender: str | None = None, city: str | None = None, phone: str | None = None) -> dict:
    """
    Returns:
        {"user": {...}}
    Raises:
        HTTPException 500 — fallo al actualizar en Supabase
    """
    from datetime import datetime, timezone
    logger.info("[auth.update_profile] id=%s", user_id)
    supabase = get_supabase()

    try:
        update_data: dict = {
            "name": name, "avatar": avatar, "bio": bio,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if gender is not None:
            update_data["gender"] = gender
        if city is not None:
            update_data["city"] = city
        if phone is not None:
            update_data["phone"] = phone
        supabase.table("profiles").update(update_data).eq("id", user_id).execute()
    except Exception as exc:
        msg = supabase_error(exc)
        logger.error("[auth.update_profile] update FAILED id=%s [%s] %s", user_id, type(exc).__name__, msg, exc_info=True)
        raise HTTPException(status_code=500, detail=msg)

    try:
        role_resp = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
        role = role_resp.data.get("role") if role_resp.data else "miembro"
    except Exception:
        role = "miembro"

    try:
        auth_user = supabase.auth.admin.get_user_by_id(user_id)
        email = auth_user.user.email if auth_user.user else None
    except Exception as exc:
        logger.warning("[auth.update_profile] auth email fetch failed id=%s [%s]", user_id, supabase_error(exc))
        email = None

    logger.info("[auth.update_profile] OK id=%s", user_id)
    if all([name, avatar, bio, gender, city, phone]):
        _award(user_id, "profile_completed")
    return {"user": {
        "id": user_id, "name": name, "email": email, "role": role,
        "avatar": avatar, "bio": bio,
        "gender": gender, "city": city, "phone": phone,
    }}

import hashlib
import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.cache import cache_delete, cache_get, cache_set
from app.core.config import get_settings
from app.core.supabase import get_supabase

logger = logging.getLogger(__name__)

# TTLs cortos para evitar pegarle a Supabase en cada request (ver gotcha #3 de
# CLAUDE.md: get_user() y la lectura de profiles son llamadas remotas).
_TOKEN_CACHE_TTL = 60
_PROFILE_CACHE_TTL = 60


def _token_cache_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"auth:token:{digest}"


def invalidate_profile_cache(user_id: str) -> None:
    """Invalida el caché de role/subscription_status de un usuario (p.ej. tras aprobar un pago)."""
    cache_delete(f"auth:profile:{user_id}")


async def get_current_user(authorization: str = Header(...)) -> dict:
    """
    Verifica el JWT del header Authorization y devuelve el usuario autenticado.

    Usage:
        @router.post("/")
        def endpoint(current_user: dict = Depends(get_current_user)):
            user_id = current_user["id"]

    Raises:
        HTTPException 401 — token ausente, con formato incorrecto, inválido o expirado
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Formato de token inválido. Usar: Bearer <token>")

    token = authorization.removeprefix("Bearer ")

    cache_key = _token_cache_key(token)
    cached_user = cache_get(cache_key)
    if cached_user is not None:
        return cached_user

    try:
        supabase = get_supabase()
        response = supabase.auth.get_user(token)
        if not response.user:
            raise HTTPException(status_code=401, detail="Token inválido o expirado")
        user = {"id": response.user.id, "email": response.user.email}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[deps.get_current_user] token validation FAILED [%s] %s", type(exc).__name__, str(exc))
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    cache_set(cache_key, user, _TOKEN_CACHE_TTL)
    return user


def _get_cached_profile(supabase, user_id: str) -> Optional[dict]:
    """Lee {role, subscription_status} de profiles, cacheado en Redis (no-op sin Redis)."""
    cache_key = f"auth:profile:{user_id}"
    profile = cache_get(cache_key)
    if profile is not None:
        return profile

    try:
        result = (
            supabase.table("profiles")
            .select("role, subscription_status")
            .eq("id", user_id)
            .single()
            .execute()
        )
        profile = result.data
    except Exception as exc:
        logger.warning("[deps._get_cached_profile] fetch failed user_id=%s [%s] %s", user_id, type(exc).__name__, str(exc))
        return None

    if profile:
        cache_set(cache_key, profile, _PROFILE_CACHE_TTL)
    return profile


async def get_current_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """
    Como get_current_user pero además exige role = 'admin' en profiles.

    Raises:
        HTTPException 403 — usuario autenticado pero sin rol admin
    """
    supabase = get_supabase()
    profile = _get_cached_profile(supabase, current_user["id"])

    if not profile or profile.get("role") != "admin":
        raise HTTPException(status_code=403, detail="No tienes permisos de administrador.")

    return current_user


async def get_active_user(current_user: dict = Depends(get_current_user)) -> dict:
    """
    Como get_current_user pero además exige subscription_status = 'active' en profiles.
    Los usuarios con role = 'invitado' o 'admin' quedan exentos de esta validación.

    Usar en las rutas que representan "la app" (lo que un usuario sin
    suscripción activa no debería poder usar) en lugar de get_current_user.

    Raises:
        HTTPException 403 — perfil no encontrado o suscripción no activa
    """
    supabase = get_supabase()
    profile = _get_cached_profile(supabase, current_user["id"])

    if not profile:
        raise HTTPException(status_code=403, detail="No se pudo verificar tu suscripción.")

    if profile.get("role") in ("invitado", "admin"):
        return current_user

    if profile.get("subscription_status") != "active":
        raise HTTPException(status_code=403, detail="Tu suscripción no está activa. Actualiza tu plan para continuar.")

    return current_user


_bearer = HTTPBearer()


def require_service_role(credentials: HTTPAuthorizationCredentials = Security(_bearer)) -> None:
    """
    Dependency para endpoints internos llamados por pg_cron.
    Acepta el service_role_key de Supabase como Bearer token — nunca expira,
    sin necesidad de JWT de usuario.
    """
    s = get_settings()
    if credentials.credentials != s.supabase_service_role_key:
        raise HTTPException(status_code=403, detail="Acceso no autorizado.")


async def get_optional_user(authorization: Optional[str] = Header(None)) -> Optional[dict]:
    """
    Igual que get_current_user pero devuelve None si no hay token en lugar de lanzar 401.
    Útil para endpoints públicos que personalizan la respuesta cuando hay sesión.
    """
    if not authorization:
        return None
    try:
        return await get_current_user(authorization)
    except HTTPException:
        return None

from functools import lru_cache

from supabase import Client, ClientOptions, create_client

from app.core.config import get_settings
from app.core.http_resilience import install as install_http_resilience

install_http_resilience()


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    settings = get_settings()
    if not settings.is_supabase_configured():
        raise RuntimeError("SUPABASE_NOT_CONFIGURED")
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        options=ClientOptions(
            auto_refresh_token=False,
            persist_session=False,
        ),
    )


def create_anon_client() -> Client:
    """Returns a fresh, non-cached client for user-session operations (sign_in, etc.).
    Must not share state with the admin singleton to avoid session contamination."""
    settings = get_settings()
    if not settings.is_supabase_configured():
        raise RuntimeError("SUPABASE_NOT_CONFIGURED")
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        options=ClientOptions(
            auto_refresh_token=False,
            persist_session=False,
        ),
    )


def is_supabase_configured() -> bool:
    return get_settings().is_supabase_configured()

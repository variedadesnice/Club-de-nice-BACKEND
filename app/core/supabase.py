from functools import lru_cache

from supabase import Client, ClientOptions, create_client

from app.core.config import get_settings


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


def is_supabase_configured() -> bool:
    return get_settings().is_supabase_configured()

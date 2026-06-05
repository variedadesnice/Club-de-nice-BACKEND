from supabase import create_client, Client
from functools import lru_cache
from lib.env import get_supabase_env, is_supabase_configured


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    if not is_supabase_configured():
        raise RuntimeError("SUPABASE_NOT_CONFIGURED")
    env = get_supabase_env()
    return create_client(env["url"], env["service_role_key"])

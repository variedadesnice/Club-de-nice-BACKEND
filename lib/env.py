import os
from urllib.parse import urlparse


def get_supabase_env():
    return {
        "url": os.getenv("SUPABASE_URL", "").strip(),
        "service_role_key": os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
    }


def is_supabase_configured() -> bool:
    env = get_supabase_env()
    url, key = env["url"], env["service_role_key"]
    if not url or not key:
        return False
    if "your-project-id" in url or "your-service-role" in key:
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.hostname.endswith(".supabase.co")
    except Exception:
        return False

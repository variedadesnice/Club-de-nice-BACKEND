from functools import lru_cache
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    gemini_api_key: str = ""
    redis_url: str = ""
    port: int = 8000
    sendhook_api_url: str = ""
    sendhook_api_key: str = ""
    # Secreto whsec_... para validar la firma de los webhooks entrantes de
    # SendHook. Es un secreto aparte de la api_key: uno protege lo que
    # mandamos, el otro valida lo que recibimos. Sin él, el receptor de
    # webhooks rechaza todo (no se aceptan eventos sin firma verificada).
    sendhook_webhook_secret: str = ""

    # Email — Resend
    resend_api_key: str = ""
    from_email: str = "El Club de Nice <hola@elclubdenice.com>"
    app_url: str = "https://elclubdenice.com"
    app_name: str = "El Club de Nice"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def is_redis_configured(self) -> bool:
        return bool(self.redis_url)

    def is_email_configured(self) -> bool:
        return bool(self.resend_api_key)

    def is_supabase_configured(self) -> bool:
        url = self.supabase_url
        key = self.supabase_service_role_key
        if not url or not key:
            return False
        if "your-project-id" in url or "your-service-role" in key:
            return False
        try:
            parsed = urlparse(url)
            return (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.hostname.endswith(".supabase.co")
            )
        except Exception:
            return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

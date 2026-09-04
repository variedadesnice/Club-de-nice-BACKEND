import logging
import threading
import time

from fastapi import HTTPException, Request

from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)


def rate_limiter(max_requests: int, window_seconds: int, prefix: str):
    """
    Dependencia de FastAPI que limita requests por IP usando una ventana fija en Redis.

    Si Redis no está configurado o falla, no limita (degrada de forma transparente).

    Usage:
        @router.post("/login", dependencies=[Depends(rate_limiter(5, 60, "login"))])
    """
    def dependency(request: Request) -> None:
        redis_client = get_redis()
        if redis_client is None:
            return

        client_ip = request.client.host if request.client else "unknown"
        window = int(time.time()) // window_seconds
        key = f"ratelimit:{prefix}:{client_ip}:{window}"

        try:
            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, window_seconds)
        except Exception as exc:
            logger.warning("[rate_limit.%s] FAILED [%s] %s", prefix, type(exc).__name__, str(exc))
            return

        if count > max_requests:
            logger.warning("[rate_limit.%s] BLOCKED ip=%s count=%d", prefix, client_ip, count)
            raise HTTPException(status_code=429, detail="Demasiados intentos. Intenta de nuevo más tarde.")

    return dependency


# Fallback en memoria para el limitador por usuario: {clave: (ventana, conteo)}.
# Es por proceso, así que con varios workers el límite real puede ser algo más
# holgado. Alcanza para lo que protege (que un usuario no dispare llamadas
# ilimitadas a un proveedor externo desde un botón) y evita quedarse sin
# ningún tope cuando Redis no está disponible.
_memory_buckets: dict = {}
_memory_lock = threading.Lock()


def _memory_incr(key: str, window: int) -> int:
    with _memory_lock:
        stored_window, count = _memory_buckets.get(key, (window, 0))
        if stored_window != window:
            count = 0
        count += 1
        _memory_buckets[key] = (window, count)
        if len(_memory_buckets) > 10000:  # poda barata, no crece sin límite
            for k, (w, _) in list(_memory_buckets.items()):
                if w != window:
                    _memory_buckets.pop(k, None)
        return count


def check_user_rate_limit(user_id: str, max_requests: int, window_seconds: int, prefix: str) -> None:
    """
    Limita por usuario autenticado, no por IP.

    Para acciones que un usuario dispara a mano y que cuestan una llamada a un
    proveedor externo, la IP es la clave equivocada: detrás de un mismo NAT
    móvil hay muchos usuarios distintos, y un mismo usuario cambia de IP al
    saltar de wifi a datos. Se usa Redis si está, y si no un contador en
    memoria — a diferencia de `rate_limiter`, acá no se degrada a "sin límite".

    Raises:
        HTTPException 429 — se pasó del cupo en la ventana actual.
    """
    window = int(time.time()) // window_seconds
    key = f"ratelimit:{prefix}:{user_id}:{window}"

    redis_client = get_redis()
    count = None
    if redis_client is not None:
        try:
            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, window_seconds)
        except Exception as exc:
            logger.warning("[rate_limit.%s] redis FAILED [%s] %s", prefix, type(exc).__name__, str(exc))
            count = None

    if count is None:
        count = _memory_incr(key, window)

    if count > max_requests:
        logger.info("[rate_limit.%s] BLOCKED user_id=%s count=%d", prefix, user_id, count)
        espera = window_seconds - (int(time.time()) % window_seconds)
        unidad = "segundo" if espera == 1 else "segundos"
        raise HTTPException(
            status_code=429,
            detail=f"Ya consultamos varias veces seguidas. Espera {espera} {unidad} y vuelve a intentarlo.",
        )
